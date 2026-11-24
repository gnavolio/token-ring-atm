"""Il nodo ATM: thread, macchina a stati, rete (P1-04...P1-17, P1-I3...P1-I5).

Vedi la docstring di ``AtmNode`` per la struttura dei thread e il motivo per cui solo il
thread principale tocca il token e il saldo.
"""

from __future__ import annotations

import dataclasses
import queue
import socket
import sys
import threading
import time
from typing import Final, Sequence

from .config import (EXIT_INTERNAL_ERROR, EXIT_OK, EXIT_PORT_IN_USE, EXIT_RING_MISCONFIGURED,
                     HOST, NodeConfig)
from .logfmt import NodeLogger
from .protocol import (MAX_LINE_BYTES, ProtocolError, Token, create_initial_token,
                       decode_message, encode_token, make_forward_token)
from .transactions import (CMD_HELP, CMD_OPERATION, CMD_QUIT, CMD_STATUS, ParseError,
                           Transaction, apply_transaction, parse_command)

CONNECT_TIMEOUT: Final[float] = 1.0     # timeout di un tentativo di connessione al successore
RETRY_INTERVAL: Final[float] = 0.5      # pausa tra due tentativi
WAIT_LOG_INTERVAL: Final[float] = 5.0   # intervallo minimo tra due righe CONNESSIONE_ATTESA
POLL_INTERVAL: Final[float] = 0.2       # attesa massima di una get() sulla inbox (Ctrl+C reattivo)

# Timeout di sendall verso il successore: un invio non resta mai bloccato all'infinito.
_SEND_TIMEOUT: Final[float] = 5.0
# Quantità massima di byte letti con una recv dal predecessore.
_RECV_CHUNK: Final[int] = 4096
# Testo esatto del campo "uso" dell'evento AIUTO; le barre sono letterali.
_HELP_USAGE: Final[str] = "deposito N | prelievo N | stato | aiuto | esci"

# Motivi registrati nell'evento ARRESTO.
_REASON_ROUNDS: Final[str] = "giri_completati"
_REASON_QUIT: Final[str] = "comando_esci"
_REASON_INTERRUPT: Final[str] = "interruzione"
_REASON_ERROR: Final[str] = "errore"

# Esito di un possesso o del ciclo principale: (motivo di ARRESTO, codice d'uscita).
_Outcome = tuple[str, int]


class AtmNode:
    """Un nodo dell'anello Token Ring: possiede al più il token e, con esso, il saldo.

    **Tre thread, un solo proprietario del token.**

    - Thread **principale** (``run``): è l'unico esecutore della macchina a stati. Prende in
      carico il token, esegue al più una transazione, applica il ritardo e inoltra il token
      al successore. È l'**unico** thread che legge o scrive il token e quindi il saldo.
    - Thread di **ascolto** (``_listen_loop``, daemon): accetta la connessione del
      predecessore, separa i messaggi per righe, li decodifica e mette i ``Token`` nella
      ``inbox``. Non elabora mai il token: lo consegna soltanto.
    - Thread della **tastiera** (``_keyboard_loop``, daemon): legge i comandi e accoda le
      transazioni in ``pending``. Non vede mai il saldo.

    **Strutture condivise tra i thread dello stesso processo** (nessuna tra processi):

    - ``inbox`` (``queue.Queue[Token]``): ascolto -> principale. È il punto di passaggio del
      token dalla rete al thread che lo possiede;
    - ``pending`` (``queue.Queue[Transaction]``, FIFO): richieste in attesa, prima quelle dello
      scenario poi quelle da tastiera. Contiene solo richieste, mai saldi;
    - ``stop_requested`` (``threading.Event``): impostato dal comando ``esci``;
    - ``stopping`` (``threading.Event``): impostato dal thread principale all'inizio
      dell'arresto, per far uscire in silenzio il thread di ascolto;
    - ``holding``, ``last_seq``, ``last_seen_balance``: stato **solo informativo** per il
      comando ``stato``, protetto da un lock. ``last_seen_balance`` non viene mai usato per
      calcolare nulla: l'unico saldo autorevole è quello nel token (P1-I1).

    Perché solo il thread principale tocca il token: la mutua esclusione tra i 4 processi è
    data dall'esistenza di un solo token; dentro il processo, avere un solo thread che lo
    maneggia rende ogni possesso sequenziale (presa in carico -> transazione -> inoltro)
    senza bisogno di lock sul saldo.
    """

    def __init__(self, config: NodeConfig, logger: NodeLogger,
                 initial_queue: Sequence[Transaction] = ()) -> None:
        """Prepara code, eventi e stato; ``initial_queue`` sono le transazioni dello scenario."""
        self._config = config
        self._logger = logger
        self.inbox: queue.Queue[Token] = queue.Queue()
        self.pending: queue.Queue[Transaction] = queue.Queue()
        for tx in initial_queue:
            self.pending.put(tx)
        self.stop_requested = threading.Event()
        self.stopping = threading.Event()
        # Stato informativo per il comando "stato" (scritto dal principale, letto dalla tastiera).
        self._info_lock = threading.Lock()
        self.holding = False
        self.last_seq: int | None = None
        self.last_seen_balance: int | None = None
        # Token attualmente in mano (solo thread principale); None fuori da un possesso.
        self._held_token: Token | None = None
        # Contatori per --giri: inoltri (nodo non creatore) e ricezioni (creatore).
        self._forwards = 0
        self._receptions = 0
        # Socket: ascolto, connessione verso il successore, connessione dal predecessore.
        self._listen_sock: socket.socket | None = None
        self._successor_sock: socket.socket | None = None
        self._conn_lock = threading.Lock()
        self._predecessor_conn: socket.socket | None = None

    # ------------------------------------------------------------------ ciclo di vita

    def run(self) -> int:
        """Esegue la seconda parte dell'avvio, il ciclo principale e l'arresto ordinato.

        Avvio: porta di ascolto, thread di ascolto e tastiera, connessione al successore,
        eventuale creazione del token. Restituisce il codice d'uscita (costanti ``EXIT_*``
        di ``config``). Registra sempre ``ARRESTO`` come ultimo
        evento, qualunque sia il motivo dell'uscita.
        """
        try:
            reason, code = self._run_states()
        except KeyboardInterrupt:
            # Ctrl+C è il modo normale di fermare la demo: arresto volontario, codice 0.
            reason, code = _REASON_INTERRUPT, EXIT_OK
        except Exception as exc:  # nessuna eccezione fa sparire il token in silenzio
            self._log_internal_error(exc)
            reason, code = _REASON_ERROR, EXIT_INTERNAL_ERROR
        return self._shutdown(reason, code)

    def _run_states(self) -> _Outcome:
        """Socket di ascolto, thread, connessione al successore, token iniziale, ciclo del token."""
        config = self._config
        try:
            self._open_listener()
        except OSError as exc:
            code = getattr(exc, "winerror", None) or exc.errno
            self._logger.log("ERRORE", tipo="porta_occupata",
                             dettaglio=f"porta {config.port} occupata o non utilizzabile ({code})")
            return _REASON_ERROR, EXIT_PORT_IN_USE
        self._logger.log("IN_ASCOLTO", porta=config.port)
        self._start_threads()

        if not self._connect_to_successor():
            return _REASON_QUIT, EXIT_OK  # "esci" mentre si attendeva il successore

        if config.create_token:
            # Creazione del token (P1-14, P1-18, P1-I3). Avviene una sola volta, solo nel nodo
            # avviato con --crea-token (uno solo nell'anello), e solo DOPO CONNESSIONE_OK:
            # così il token nasce quando c'è già un successore a cui passarlo e non resta
            # fermo in un nodo che non può inoltrarlo. Nessun altro punto del codice crea
            # token: per questo nell'anello ne esiste esattamente uno.
            token = create_initial_token(config.node_id, config.initial_balance)
            self._logger.log("TOKEN_CREATO", seq=token.seq, saldo=token.balance)
            outcome = self._handle_token(token, received=False)
            if outcome is not None:
                return outcome
        return self._wait_for_tokens()

    def _wait_for_tokens(self) -> _Outcome:
        """Stato ``ATTESA_TOKEN``: preleva i token dalla ``inbox`` e li gestisce uno alla volta."""
        config = self._config
        while True:
            try:
                # Mai attese senza timeout: ogni POLL_INTERVAL il thread torna all'interprete,
                # così Ctrl+C viene gestito anche su Windows.
                token = self.inbox.get(timeout=POLL_INTERVAL)
            except queue.Empty:
                # "esci" agisce qui, solo con la inbox vuota e nessun token in mano: tra un
                # possesso e l'altro, quindi il comando non porta via un token in mano.
                # Resta solo la finestra minima di un token arrivato proprio nell'istante
                # dell'arresto: viene registrato come TOKEN_PERSO (limite noto, nessuna
                # rigenerazione del token).
                if self.stop_requested.is_set():
                    return _REASON_QUIT, EXIT_OK
                continue
            if token.receiver != config.node_id:
                # Errore di configurazione: il predecessore è stato avviato con la porta del
                # successore sbagliata e ci ha mandato un token destinato a un altro nodo. Il token
                # non viene preso in carico: lo si segnala e ci si ferma con codice 5.
                self._logger.log(
                    "ERRORE", tipo="destinatario_errato",
                    dettaglio=(f"token per {token.receiver} ricevuto da {token.sender}: "
                               "controllare --porta-successore del predecessore"))
                return _REASON_ERROR, EXIT_RING_MISCONFIGURED
            # Il campo sender serve solo al log (da=): il nodo non lo usa mai per inviare (P1-09).
            self._logger.log("TOKEN_RICEVUTO", seq=token.seq, saldo=token.balance,
                             da=token.sender)
            outcome = self._handle_token(token, received=True)
            if outcome is not None:
                return outcome

    # ------------------------------------------------------------------ gestione del token

    def _set_held(self, token: Token | None) -> None:
        """Aggiorna il token in mano e lo stato informativo letto dal comando ``stato``."""
        self._held_token = token
        with self._info_lock:
            if token is not None:
                self.holding = True
                self.last_seq = token.seq
            else:
                self.holding = False

    def _release(self, balance: int) -> None:
        """Fine del possesso: nessun token in mano; ricorda il saldo visto (solo informativo)."""
        self._set_held(None)
        with self._info_lock:
            self.last_seen_balance = balance

    def _handle_token(self, token: Token, received: bool) -> _Outcome | None:
        """Un possesso completo del token: al più una transazione, ritardo, inoltro.

        Sequenza (P1-08, P1-16, P1-I5):
        1. presa in carico: da qui il nodo ha il diritto esclusivo di toccare il saldo;
        2. ``--giri`` sul creatore: all'N-esimo rientro il token viene **ritirato**
           (``TOKEN_RITIRATO``) senza transazioni né inoltro, ed è l'unico modo in cui il
           token smette di circolare in un'esecuzione normale (usato nei test);
        3. si estrae dalla coda **una sola** richiesta (``get_nowait`` chiamata una volta):
           al massimo una transazione per possesso, le altre aspettano il giro successivo;
           con la coda vuota si passa subito all'inoltro (``NESSUNA_TRANSAZIONE``, P1-17);
        4. sezione critica (``_execute_critical_section``);
        5. inoltro (``_forward_token``), sempre e solo **dopo** la fine della transazione.

        Restituisce ``(motivo, codice)`` se il nodo deve fermarsi, altrimenti None.
        """
        config = self._config
        self._set_held(token)
        if received and config.create_token and config.rounds > 0:
            self._receptions += 1
            if self._receptions == config.rounds:
                self._logger.log("TOKEN_RITIRATO", seq=token.seq, saldo=token.balance,
                                 giri=config.rounds)
                self._release(token.balance)
                return _REASON_ROUNDS, EXIT_OK
        try:
            tx: Transaction | None = self.pending.get_nowait()
        except queue.Empty:
            tx = None
        if tx is None:
            self._logger.log("NESSUNA_TRANSAZIONE", seq=token.seq, saldo=token.balance)
        else:
            token = self._execute_critical_section(token, tx)
        return self._forward_token(token)

    def _execute_critical_section(self, token: Token, tx: Transaction) -> Token:
        """Sezione critica: esegue una transazione sul saldo del token in mano (P1-12, P1-13).

        Perché è mutuamente esclusiva: questa funzione è chiamata solo da ``_handle_token``,
        nel thread principale, con un token appena preso in carico. Esiste un solo token
        nell'anello (P1-14) e il saldo esiste solo dentro di esso (P1-I1): mentre il token
        è qui, nessun altro processo ha un saldo da leggere o scrivere, e nessun altro thread
        di questo processo lo tocca.

        Perché è atomica (P1-13): tra il passo 1 e il passo 5 non ci sono attese, rete o
        letture di input, solo calcolo e log. Il token viene inoltrato solo dopo il passo 5,
        quindi nessun nodo può mai vedere un saldo intermedio. Se il processo viene interrotto
        qui (Ctrl+C) il token non esce dal nodo e nessun saldo parziale si propaga.

        Restituisce il nuovo token con il saldo aggiornato (o invariato se rifiutata).
        """
        # Passo 1 - lettura: il saldo corrente si legge solo dal token in mano.
        balance = token.balance
        self._logger.log("INIZIO_TRANSAZIONE", seq=token.seq, saldo=balance, op=tx.op,
                         importo=tx.amount)
        # Passo 2 - validazione: per un prelievo l'importo non deve superare il saldo.
        result = apply_transaction(balance, tx)
        # Passo 3 - aggiornamento: nuovo saldo calcolato da apply_transaction (invariato se
        # la transazione è rifiutata, P1-I2).
        if result.accepted:
            self._logger.log("SALDO_AGGIORNATO", seq=token.seq, saldo=result.new_balance,
                             prima=result.old_balance,
                             variazione=f"{result.new_balance - result.old_balance:+d}")
        else:
            self._logger.log("TRANSAZIONE_RIFIUTATA", seq=token.seq, saldo=balance,
                             motivo=result.reason)
        # Passo 4 - scrittura: il nuovo saldo viene scritto NEL TOKEN (nuovo oggetto
        # immutabile) che verrà inoltrato; il nodo non ne conserva altra copia autorevole.
        token = dataclasses.replace(token, balance=result.new_balance)
        self._set_held(token)
        # Passo 5 - log: chiusura della sezione critica con l'esito.
        self._logger.log("FINE_TRANSAZIONE", seq=token.seq, saldo=token.balance,
                         esito="ACCETTATA" if result.accepted else "RIFIUTATA")
        return token

    def _forward_token(self, token: Token) -> _Outcome | None:
        """Stato ``INOLTRO``: ritardo di visualizzazione, verifica del successore, invio.

        Ritardo di visualizzazione (P1-I4): ``time.sleep(--ritardo)`` è la prima azione
        dell'inoltro, cioè **fuori** dalla sezione critica (che è già chiusa) e uguale per
        ogni hop. Non viola P1-17 ("inoltra immediatamente"): la decisione di inoltrare è già presa e non dipende
        da nulla; il ritardo equivale a un collegamento più lento e serve solo a rendere
        leggibile la demo. Il nodo non lo usa per raccogliere richieste: un comando digitato
        ora finisce in coda per il possesso successivo. Con ``--ritardo 0`` l'inoltro è
        letteralmente immediato.

        Ordine ``TOKEN_INOLTRATO`` -> ``sendall``: la riga di inoltro ha sempre un timestamp
        precedente alla ``TOKEN_RICEVUTO`` del successore (causalità leggibile nei log).

        Reinvio senza duplicazione (P1-14): se il successore non è raggiungibile (verifica
        fallita o ``sendall`` in errore) si registra ``SUCCESSORE_PERSO``, ci si riconnette e
        si reinvia lo **stesso** token (stesso ``seq``). Il token non può duplicarsi: l'invio
        precedente non è mai partito, oppure è fallito; un frammento parziale senza ``\\n``
        viene scartato dal ricevente. Durante la riconnessione il token resta in mano.

        Restituisce ``(motivo, codice)`` se il nodo deve fermarsi, altrimenti None.
        """
        config = self._config
        time.sleep(config.delay)
        outgoing = make_forward_token(token, config.node_id, config.successor_id)
        data = encode_token(outgoing)
        while True:
            sock = self._successor_sock
            if sock is not None and self._successor_alive():
                self._logger.log("TOKEN_INOLTRATO", seq=outgoing.seq, saldo=outgoing.balance,
                                 a=config.successor_id)
                try:
                    sock.sendall(data)
                except OSError:
                    pass  # invio fallito: si tratta come connessione persa
                else:
                    break
            self._logger.log("SUCCESSORE_PERSO", successore=config.successor_id,
                             porta_successore=config.successor_port)
            self._close_successor()
            if not self._connect_to_successor():
                # "esci" durante la riconnessione: l'anello è già fermo perché il successore
                # non risponde, e l'operatore sceglie di chiudere. Il token è ancora in mano e
                # verrà registrato come TOKEN_PERSO dalla sequenza di arresto.
                return _REASON_QUIT, EXIT_OK
        self._release(outgoing.balance)
        self._forwards += 1
        if not config.create_token and config.rounds > 0 and self._forwards == config.rounds:
            return _REASON_ROUNDS, EXIT_OK  # --giri N: N-esimo inoltro di un nodo non creatore
        return None

    # ------------------------------------------------------------------ rete: successore

    def _connect_to_successor(self) -> bool:
        """Apre la connessione persistente verso il successore, riprovando senza limite (P1-I3).

        Una sola connessione TCP per tutto il funzionamento: niente connect/close per hop e
        un fatto osservabile (``CONNESSIONE_OK``) per dire che il successore risponde. Se il
        successore non è ancora avviato, ogni tentativo dura al più ``CONNECT_TIMEOUT`` ed è
        seguito da una pausa di ``RETRY_INTERVAL``: i nodi si possono avviare in qualsiasi
        ordine. ``CONNESSIONE_ATTESA`` viene registrato al primo fallimento e poi al più ogni
        ``WAIT_LOG_INTERVAL`` secondi, per non riempire la finestra della demo.

        Prima di ogni tentativo si controlla ``stop_requested`` (comando ``esci``): mentre si
        aspetta il successore l'anello è già fermo, quindi ``esci`` viene eseguito subito.

        Restituisce True se connesso, False se è stato richiesto l'arresto con ``esci``.
        """
        config = self._config
        attempt = 0
        last_wait_log: float | None = None
        while True:
            if self.stop_requested.is_set():
                return False
            attempt += 1
            try:
                sock = socket.create_connection((HOST, config.successor_port),
                                                timeout=CONNECT_TIMEOUT)
            except OSError:
                now = time.monotonic()
                if last_wait_log is None or now - last_wait_log >= WAIT_LOG_INTERVAL:
                    self._logger.log("CONNESSIONE_ATTESA", successore=config.successor_id,
                                     porta_successore=config.successor_port,
                                     tentativo=attempt)
                    last_wait_log = now
                time.sleep(RETRY_INTERVAL)
                continue
            sock.settimeout(_SEND_TIMEOUT)
            self._successor_sock = sock
            self._logger.log("CONNESSIONE_OK", successore=config.successor_id,
                             porta_successore=config.successor_port, tentativi=attempt)
            return True

    def _successor_alive(self) -> bool:
        """Verifica, prima di ogni invio, che la connessione al successore sia ancora aperta.

        Tecnica: ``recv(1, MSG_PEEK)`` in modalità non bloccante. ``BlockingIOError`` significa
        "nessun dato e connessione viva"; ``b""`` significa che il successore ha chiuso;
        ``ConnectionResetError``/``OSError`` che la connessione è persa. La verifica è sicura
        perché il successore non scrive mai sulla connessione (il protocollo va in un solo
        verso): ``MSG_PEEK`` non consuma nulla e non può rubare un messaggio. Serve a non
        "inviare nel vuoto" il token a un successore che è stato chiuso e riavviato.
        """
        sock = self._successor_sock
        if sock is None:
            return False
        try:
            sock.setblocking(False)
            try:
                sock.recv(1, socket.MSG_PEEK)
            finally:
                sock.settimeout(_SEND_TIMEOUT)
        except BlockingIOError:
            return True
        except OSError:
            return False
        return False  # b"" (chiusa dal successore) o dati inattesi

    def _close_successor(self) -> None:
        """Chiude la connessione verso il successore (``shutdown(SHUT_WR)`` e poi ``close``).

        ``SHUT_WR`` prima di ``close`` fa arrivare al successore tutti i byte già inviati
        (compreso l'ultimo token) prima della fine ordinata della connessione.
        """
        sock, self._successor_sock = self._successor_sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ rete: predecessore

    def _open_listener(self) -> None:
        """Apre il socket di ascolto su ``127.0.0.1:--porta`` (backlog 1). Propaga ``OSError``.

        Su Windows si imposta ``SO_EXCLUSIVEADDRUSE`` e **non** ``SO_REUSEADDR``: su Windows
        ``SO_REUSEADDR`` permetterebbe a un secondo processo di fare ``bind`` sulla stessa
        porta, e un nodo avviato due volte (o una porta occupata) non verrebbe rilevato.
        Altrove ``SO_REUSEADDR`` permette solo di riusare una porta in ``TIME_WAIT``.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((HOST, self._config.port))
            sock.listen(1)
            # Timeout su accept: il thread di ascolto controlla "stopping" a ogni giro.
            sock.settimeout(POLL_INTERVAL)
        except OSError:
            sock.close()
            raise
        self._listen_sock = sock

    def _start_threads(self) -> None:
        """Avvia i thread daemon di ascolto e (se c'è uno stdin) della tastiera."""
        threading.Thread(target=self._listen_loop, name="ascolto", daemon=True).start()
        if sys.stdin is not None:
            threading.Thread(target=self._keyboard_loop, name="tastiera", daemon=True).start()

    def _listen_loop(self) -> None:
        """Thread di ascolto: accetta il predecessore e consegna i token alla ``inbox``.

        - **Una connessione alla volta**: il nodo ha un solo predecessore. Una connessione
          in arrivo mentre un'altra è attiva resta nel backlog e viene accettata quando la
          precedente si chiude; un predecessore riavviato viene quindi riaccettato da solo.
        - Questo thread **non elabora il token**: lo decodifica e lo mette nella ``inbox``.
          Solo il thread principale prende in carico il token e tocca il saldo, così i
          possessi restano sequenziali (vedi la docstring della classe).
        - Quando ``stopping`` è impostato esce alla prima occasione **senza registrare
          nulla**: la sequenza di arresto ha già scritto ``ARRESTO`` come ultima riga.
        """
        listener = self._listen_sock
        assert listener is not None
        while not self.stopping.is_set():
            try:
                conn, addr = listener.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self.stopping.is_set():
                    return
                self._log_thread_error(exc)
                time.sleep(RETRY_INTERVAL)
                continue
            if self.stopping.is_set():
                conn.close()
                return
            try:
                self._serve_predecessor(conn, f"{addr[0]}:{addr[1]}")
            except Exception as exc:  # errore inatteso: si chiude la connessione e si riparte
                if self.stopping.is_set():
                    return
                self._log_thread_error(exc)

    def _serve_predecessor(self, conn: socket.socket, remote: str) -> None:
        """Legge le righe di una connessione dal predecessore fino a EOF o errore.

        Framing: TCP è un flusso di byte, quindi una ``recv`` può restituire mezzo
        messaggio o più messaggi insieme. I byte si accumulano in un buffer e si separano
        sulle ``\\n``: ogni riga completa è un messaggio. Una riga più lunga di
        ``MAX_LINE_BYTES`` (4096) non può essere un token valido: si registra l'errore e si
        chiude la connessione, per non far crescere il buffer senza limite.
        """
        with self._conn_lock:
            self._predecessor_conn = conn
        try:
            conn.settimeout(POLL_INTERVAL)
            self._logger.log("PREDECESSORE_CONNESSO", remoto=remote)
            buffer = bytearray()
            oversized = False
            while not oversized:
                if self.stopping.is_set():
                    return
                try:
                    chunk = conn.recv(_RECV_CHUNK)
                except TimeoutError:
                    continue  # nessun dato: non è un errore, si ricontrolla "stopping"
                except OSError:
                    break  # ConnectionResetError e simili: come EOF, il predecessore non c'è più
                if not chunk:
                    break  # EOF: il predecessore ha chiuso la connessione
                buffer += chunk
                while not oversized:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        oversized = len(buffer) > MAX_LINE_BYTES
                        break
                    line = bytes(buffer[:newline])
                    del buffer[:newline + 1]
                    if len(line) > MAX_LINE_BYTES:
                        oversized = True
                    else:
                        self._process_line(line)
            if self.stopping.is_set():
                return
            if oversized:
                self._logger.log("ERRORE", tipo="messaggio_malformato",
                                 dettaglio=f"riga piu' lunga di {MAX_LINE_BYTES} byte")
            elif bytes(buffer).strip():
                # Byte residui senza "\n" alla chiusura: non vengono MAI decodificati. Se un
                # sendall del predecessore è fallito dopo aver trasmesso tutto tranne "\n",
                # il predecessore reinvierà lo stesso token: decodificare il frammento
                # creerebbe un secondo token (P1-14).
                self._logger.log("ERRORE", tipo="messaggio_malformato",
                                 dettaglio="messaggio incompleto alla chiusura della connessione")
            self._logger.log("PREDECESSORE_CHIUSO", remoto=remote)
        finally:
            with self._conn_lock:
                self._predecessor_conn = None
            try:
                conn.close()
            except OSError:
                pass

    def _process_line(self, line: bytes) -> None:
        """Decodifica una riga e mette il token nella ``inbox``; scarta le righe malformate.

        Una riga malformata viene registrata e scartata, ma la connessione resta aperta: un
        messaggio spurio non è un token e non deve interrompere il canale da cui arriverà
        quello vero. Le righe di soli spazi bianchi vengono ignorate senza log.
        """
        if not line.strip():
            return
        try:
            token = decode_message(line)
        except ProtocolError as exc:
            self._logger.log("ERRORE", tipo="messaggio_malformato", dettaglio=str(exc))
            return
        self.inbox.put(token)

    # ------------------------------------------------------------------ tastiera

    def _keyboard_loop(self) -> None:
        """Thread della tastiera: legge i comandi e accoda le transazioni.

        Questo thread **accoda soltanto**: non entra mai in sezione critica, non vede il
        token né il saldo. Una richiesta digitata diventa una ``Transaction`` in ``pending``
        e verrà eseguita dal thread principale al prossimo possesso del token (al più una
        per possesso). Per questo la tastiera non può mai violare la mutua esclusione.

        Un comando non valido produce ``COMANDO_NON_VALIDO`` e il thread continua (mai crash).
        Con stdin a fine file (``DEVNULL``, pipe chiusa) o in errore il thread termina senza
        registrare nulla e il nodo continua a funzionare. ``esci`` imposta solo
        ``stop_requested``: l'arresto lo esegue il thread principale nel momento sicuro.
        """
        try:
            while True:
                try:
                    line = sys.stdin.readline()
                except (OSError, ValueError):
                    return
                if not line:
                    return  # EOF
                try:
                    command = parse_command(line)
                except ParseError as exc:
                    self._logger.log("COMANDO_NON_VALIDO", motivo=exc.reason,
                                     testo=exc.text.strip())
                    continue
                if command is None:
                    continue  # riga vuota o solo commento: ignorata senza log
                if command.kind == CMD_OPERATION:
                    tx = command.transaction
                    assert tx is not None
                    self.pending.put(tx)
                    # Valore informativo: il thread principale può aver già estratto la
                    # transazione tra put e qsize, quindi in_coda può valere 0.
                    self._logger.log("COMANDO_ACCETTATO", op=tx.op, importo=tx.amount,
                                     in_coda=self.pending.qsize())
                elif command.kind == CMD_STATUS:
                    with self._info_lock:
                        holding, last_seq = self.holding, self.last_seq
                        seen_balance = self.last_seen_balance
                    self._logger.log("STATO", token=holding, in_coda=self.pending.qsize(),
                                     ultimo_seq=last_seq, saldo_visto=seen_balance)
                elif command.kind == CMD_HELP:
                    self._logger.log("AIUTO", uso=_HELP_USAGE)
                elif command.kind == CMD_QUIT:
                    self.stop_requested.set()
                    return
        except Exception as exc:  # errore inatteso: il thread della tastiera termina
            self._log_thread_error(exc)

    # ------------------------------------------------------------------ arresto

    def _apply_token_lost_rule(self) -> None:
        """Regola ``TOKEN_PERSO``: registra il token che va perso con l'arresto.

        Il token esiste solo in memoria (nessun file né stato condiviso, P1-10) e, per
        scelta, non esiste alcun meccanismo di rigenerazione o di elezione: aggiungerlo
        andrebbe oltre il solo Token Ring richiesto. Se il nodo si ferma mentre ha il token
        in mano, o già ricevuto nella ``inbox``, il token è perso e l'anello si ferma per
        sempre. Lo si registra comunque, con ``seq`` e saldo, perché sia evidente nei log
        dove e con quale saldo l'anello si è interrotto.

        Limite noto: un Ctrl+C che cade esattamente tra due istruzioni critiche (subito dopo
        un invio riuscito, o tra l'estrazione dalla ``inbox`` e la presa in carico) può
        produrre un ``TOKEN_PERSO`` per un token già inoltrato, o perdere il token senza log.
        """
        held = self._held_token
        if held is not None:
            # seq del possesso (non +1) e saldo attuale, dopo l'eventuale transazione.
            self._logger.log("TOKEN_PERSO", seq=held.seq, saldo=held.balance)
            return
        while True:
            try:
                token = self.inbox.get_nowait()
            except queue.Empty:
                return
            self._logger.log("TOKEN_PERSO", seq=token.seq, saldo=token.balance)

    def _shutdown(self, reason: str, code: int) -> int:
        """Sequenza di arresto, sempre nello stesso ordine.

        1. ``stopping`` viene impostato **prima** di chiudere i socket: quando il thread di
           ascolto si sveglia per la chiusura, sa che è un arresto e non registra
           ``PREDECESSORE_CHIUSO`` né errori;
        2. regola ``TOKEN_PERSO`` (per interruzione, ``esci`` o errore interno);
        3. ``ARRESTO`` con ``log_final``: scrittura e chiusura del log sono atomiche, quindi
           nessun altro thread può scrivere dopo: ``ARRESTO`` è sempre l'ultima riga;
        4. chiusura dei socket: successore (``SHUT_WR`` + ``close``), predecessore, ascolto.
        """
        self.stopping.set()
        if reason in (_REASON_INTERRUPT, _REASON_QUIT) or (
                reason == _REASON_ERROR and code == EXIT_INTERNAL_ERROR):
            try:
                self._apply_token_lost_rule()
            except Exception:  # il log di TOKEN_PERSO non deve impedire ARRESTO
                pass
        try:
            self._logger.log_final("ARRESTO", motivo=reason, codice=code)
        finally:
            # I socket si chiudono comunque, anche se la scrittura del log fosse fallita.
            self._close_successor()
            with self._conn_lock:
                conn, self._predecessor_conn = self._predecessor_conn, None
            for sock in (conn, self._listen_sock):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._listen_sock = None
        return code

    # ------------------------------------------------------------------ errori

    def _log_internal_error(self, exc: BaseException) -> None:
        """Registra ``ERRORE tipo=errore_interno`` senza mai sollevare a sua volta."""
        try:
            self._logger.log("ERRORE", tipo="errore_interno",
                             dettaglio=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass

    def _log_thread_error(self, exc: BaseException) -> None:
        """Come ``_log_internal_error``, ma in silenzio se l'arresto è già iniziato."""
        if not self.stopping.is_set():
            self._log_internal_error(exc)
