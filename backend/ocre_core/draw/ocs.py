"""Cliente OpenCADStudio (CAD vivo F2) via REST headless — beta v0.1.

O OpenCADStudio é GPL-3.0: ele é usado AQUI SOMENTE como servidor
externo, via HTTP em loopback (``127.0.0.1``), nunca linkado ou
importado. Nenhum código do OCS entra no OCRE. A rota de plugin
nativo Rust (``cdylib`` + ``ocs_plugin_api``) NÃO é usada (linkagem
GPL + pins exatos de rustc/acadrust).

Transporte (``docs/automation/API-SPEC.md`` v1.1, protocolo 1):
``OpenCADStudio --http <porta>`` em ``http://127.0.0.1:<porta>/api/v1``,
``Authorization: Bearer <token>`` em TODA requisição. O token vem da
variável de ambiente ``OCS_API_TOKEN`` ou de um arquivo 0600
(``OCS_TOKEN_FILE`` ou argumento); nunca vai na URL e nunca é
registrado em log.

Regras implementadas (espelham o §1 do API-SPEC):
- Idempotência por ``request_id`` (um por operação lógica; o mesmo id
  só é reutilizado para retry após timeout). Replay com o mesmo id
  devolve o resultado em cache sem executar duas vezes (provado no
  spike: ``entities_create`` repetido criou 1 entidade só).
- Concorrência otimista por ``revision``: uma mutação com revisão
  velha é recusada com ``stale_state`` (HTTP 409); o cliente atualiza
  o ``state`` e tenta UMA vez com ``request_id`` novo.
- ``batch`` (≤ 64 passos) NÃO existe no REST da build v2026.44
  (``POST /batch`` responde ``unknown_operation`` — provado no spike);
  ``run_steps`` envia os passos em sequência, um por requisição,
  parando no primeiro erro e relatando ``completed_steps`` /
  ``next_step`` com a mesma semântica. Nada aqui depende do ``batch``
  do servidor.

Evidência do spike (v2026.44, AppImage oficial, 2026-11-05):
- ``xdata_set``/``xdata_get`` com APPID próprio do Draw (``DRAW``):
  round-trip OK nos 3 traços da F0, e o ``save_verified`` preserva o
  id (reabertura + ``xdata_get`` devolve ``s-0001/2/3``; conferido
  fora do OCS com ezdxf no arquivo salvo).
- ``audit``: ``dropped_on_save == 0``, ``lossless == true`` (DXF
  R2000/AC1015). ``save_verified``: ``verified == true`` + SHA-256.
- Leitura de XDATA pelo REST documentado
  (``GET /entities/{h}/xdata/{app}``) responde ``unknown_route`` na
  v2026.44; a leitura usada aqui é a operação de envelope
  ``xdata_get`` (``POST /xdata_get``), que funciona.
- ``capture`` headless responde ``gui_required``: não há prova por
  pixel sem janela; o proxy medido do gate G2.1 (traço visível ≤ 2 s)
  é abrir → entidades consultáveis (0,045 s no spike).
- Export STEP: NÃO faz parte deste cliente. O exportador do OCS é
  malha facetada AP214 (``src/io/step.rs``: triângulos como
  ``ADVANCED_FACE`` num ``MANIFOLD_SOLID_BREP``), não B-rep analítico;
  ``--export`` e ``save`` recusam ``.step`` e ``STEPOUT`` é ligado à
  GUI (abre diálogo de arquivo; nada escrito headless). O STEP AP242
  da F4 continua sendo o worker build123d.

Apagar (borracha, CAD-finish): a API **tem** delete — op de envelope
``entities_delete`` (``{"handles": [...]}``, API-SPEC v1.1 §6.3,
``docs/automation/README.md`` "entities_delete — erase entities";
manifesto MCP ``ocs_commands`` / ``src/mcp_ops.rs`` ``ENTITIES_DELETE``;
REST ``DELETE /entities?handles=`` §6.11). Tudo-ou-nada: todo handle
tem de existir, senão nada é apagado e o servidor responde
``entity_absent`` nomeando os ausentes; o sucesso devolve
``result.erased`` (contagem) e produz um único passo de undo. O
cliente apaga SEMPRE a partir do documento ``draw.paper.v1`` (o
documento é o mestre; o CAD segue o documento): ``erase_strokes``
resolve cada ``stroke_id`` para o handle via XDATA ``DRAW`` e só
então deleta; ``sync_document`` lê as tumbstones (``status:
"erased"``) do documento, deleta o que ainda está no CAD e PROVA a
ausência com uma varredura completa de XDATA. Apagar um id
desconhecido falha fechada antes de qualquer mutação — nunca
silenciosa. Não há re-sincronização por DXF fresco aqui: o DXF da F0
já exclui os apagados, mas reabrir um DXF trocaria o documento ativo
e perderia o undo; o delete direto preserva o resto do desenho.

O cliente NÃO exige o OCS rodando para importar o módulo: sem
servidor, cada chamada falha fechada com ``OcsUnavailableError``
(clara, com o motivo). Sem token, a construção falha fechada.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

__all__ = [
    "APPID",
    "BATCH_MAX_STEPS",
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT_S",
    "ENTITY_SCAN_LIMIT",
    "LAYER",
    "OcsClient",
    "OcsError",
    "OcsUnavailableError",
    "find_ocs_binary",
    "launch_headless",
    "resolve_token",
    "stop_server",
]

APPID = "DRAW"
LAYER = "DRAW_STROKES"
DEFAULT_PORT = 8090
DEFAULT_TIMEOUT_S = 60.0
BATCH_MAX_STEPS = 64
_PROTOCOL = 1
# Limite pedido nas varreduras de entidades. A API do OCS pagina por
# ``limit`` e não expõe offset: uma resposta com exatamente ``limit``
# itens pode estar truncada e uma varredura truncada NÃO prova
# completude (um traço fora da janela seria invisível ao scan —
# verified_absent viraria mentira). O limite é alto justamente para a
# resposta cheia ser prova de que nada ficou de fora.
ENTITY_SCAN_LIMIT = 10000
# Tolerância do pareamento por geometria (mm): coordenadas que
# sobem ao OCS pelo DXF e voltam via JSON chegam praticamente exatas;
# qualquer divergência maior significa geometria alterada — e entidade
# alterada não pode ser rotulada com segurança.
_GEOMETRY_TOLERANCE_MM = 1e-6


class OcsError(RuntimeError):
    """Erro do OpenCADStudio (resposta do servidor ou recusa local).

    ``code`` é o código simbólico do OCS (ex.: ``stale_state``,
    ``entity_absent``, ``save_failed``) ou um código local
    (``unavailable``, ``no_token``, ``bad_token_file``,
    ``not_loopback``).
    """

    def __init__(self, message: str, *, code: str = "error"):
        super().__init__(message)
        self.code = code


class OcsUnavailableError(OcsError):
    """O servidor OCS não está acessível (não está rodando / sem rota).

    Falha fechada e clara: o chamador decide se isso é skip honesto
    ou erro, mas nunca recebe um sucesso fingido.
    """

    def __init__(self, message: str):
        super().__init__(message, code="unavailable")


def resolve_token(
    token: str | None = None, token_file: str | Path | None = None
) -> str:
    """Resolve o Bearer token do OCS sem nunca expô-lo em URL ou log.

    Precedência: argumento ``token`` > ``OCS_API_TOKEN`` > arquivo
    ``token_file`` / ``OCS_TOKEN_FILE``. O arquivo DEVE ser modo 0600
    (recusado caso contrário). Sem nenhuma fonte, falha fechada.
    """
    if token:
        return token
    env_token = os.environ.get("OCS_API_TOKEN")
    if env_token:
        return env_token
    env_file = os.environ.get("OCS_TOKEN_FILE")
    if token_file is None and not env_file:
        raise OcsError(
            "sem token do OpenCADStudio: defina OCS_API_TOKEN, "
            "OCS_TOKEN_FILE (arquivo 0600) ou passe token=",
            code="no_token",
        )
    path = Path(token_file or env_file or "")
    if not path.is_file():
        raise OcsError(
            f"arquivo de token do OCS não encontrado: {path}",
            code="no_token",
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise OcsError(
            f"arquivo de token do OCS deve ser modo 0600 (está {oct(mode)}): {path}",
            code="bad_token_file",
        )
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise OcsError(f"arquivo de token do OCS vazio: {path}", code="no_token")
    return value


def find_ocs_binary(binary: str | None = None) -> str | None:
    """Devolve o caminho do binário ``OpenCADStudio`` ou ``None``.

    Precedência: argumento > ``OCS_BINARY`` > PATH. ``None`` é o skip
    honesto do teste de integração (binário ausente, não falha).
    """
    if binary:
        return binary
    env_binary = os.environ.get("OCS_BINARY")
    if env_binary:
        return env_binary
    return shutil.which("OpenCADStudio")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def launch_headless(
    binary: str,
    *,
    port: int | None = None,
    token: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[subprocess.Popen, "OcsClient"]:
    """Sobe o OCS headless (``--http <porta>``) e espera o ``/ready``.

    Devolve ``(processo, cliente)``. O chamador deve encerrar com
    :func:`stop_server`. O token vai por variável de ambiente do
    processo filho, nunca na linha de comando.
    """
    port = port if port is not None else _free_port()
    resolved = resolve_token(token)
    env = dict(os.environ)
    env["OCS_API_TOKEN"] = resolved
    try:
        process = subprocess.Popen(
            [binary, "--http", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise OcsUnavailableError(
            f"não foi possível executar o binário do OCS ({binary}): {exc}"
        ) from exc
    client = OcsClient(port=port, token=resolved, timeout_s=timeout_s)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if process.poll() is not None:
            raise OcsUnavailableError(
                f"servidor OCS saiu antes do ready (código {process.returncode})"
            )
        try:
            client.ready()
            return process, client
        except OcsError:
            time.sleep(0.25)
    stop_server(process)
    raise OcsUnavailableError(
        f"servidor OCS não ficou ready em {timeout_s}s na porta {port}"
    )


def stop_server(process: subprocess.Popen) -> None:
    """Encerra um servidor lançado por :func:`launch_headless`."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


class OcsClient:
    """Cliente REST do OpenCADStudio headless (loopback + Bearer).

    Importar e construir o cliente NÃO fala com o servidor; só cada
    chamada faz HTTP. Sem servidor, cada chamada levanta
    ``OcsUnavailableError``.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        host: str = "127.0.0.1",
        token: str | None = None,
        token_file: str | Path | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise OcsError(
                f"o cliente OCS só fala com loopback (host recusado: {host})",
                code="not_loopback",
            )
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._token = resolve_token(token, token_file)
        self.base_url = f"http://{host}:{port}/api/v1"

    # -- HTTP baixo nível -------------------------------------------------

    def _request(
        self, method: str, path: str, body: dict | list | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                raw = resp.read()
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError as exc:
                    raise OcsError(
                        f"resposta de {method} {path} não é JSON: "
                        f"{raw[:200]!r}",
                        code="bad_response",
                    ) from exc
                if not isinstance(parsed, dict):
                    raise OcsError(
                        f"resposta de {method} {path} não é um objeto "
                        f"JSON: {parsed!r}",
                        code="bad_response",
                    )
                return resp.status, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {}
            raise _error_from_body(parsed, exc.code) from exc
        except TimeoutError as exc:
            raise OcsUnavailableError(
                f"OpenCADStudio não respondeu em {self.timeout_s}s "
                f"({method} {path})"
            ) from exc
        except OSError as exc:
            raise OcsUnavailableError(
                f"OpenCADStudio não acessível em {self.base_url} "
                f"(servidor fora do ar?): {exc}"
            ) from exc

    def _envelope(
        self, op: str, params: dict | None = None, *, request_id: str | None = None,
        unwrap: str = "result",
    ) -> dict:
        """Uma operação de envelope, com UM retry em ``stale_state``.

        Busca o ``state`` atual, reenvia com ``revision`` fresca e
        ``request_id`` NOVO (o id velho já pode estar no cache do
        servidor com a recusa). Se a segunda tentativa também recusar,
        levanta.

        ``unwrap="result"`` extrai ``result`` (a maioria das mutações);
        ``unwrap="body"`` devolve o corpo inteiro (``audit`` e
        ``xdata_get`` respondem sem chave ``result`` na v2026.44).
        """
        reserved = {"protocol", "op", "request_id", "document_id"}
        if params is not None and not isinstance(params, dict):
            raise ValueError(f"params deve ser um dict: {params!r}")
        if params and reserved & params.keys():
            # Um params com essas chaves sobrescreveria o envelope de
            # silêncio — op divergente da rota ou request_id trocado
            # quebram a idempotência sem erro visível.
            raise ValueError(
                f"params não pode conter chaves reservadas do "
                f"envelope {sorted(reserved)}: {sorted(reserved & params.keys())}"
            )
        body: dict[str, Any] = {
            "protocol": _PROTOCOL,
            "op": op,
            "request_id": request_id or _new_request_id(),
            "document_id": self._document_id(),
        }
        body.update(params or {})
        try:
            return self._call_envelope(op, body, unwrap)
        except OcsError as exc:
            if exc.code != "stale_state":
                raise
            state = self.state()
            body["request_id"] = _new_request_id()
            body["document_id"] = state.get("document_id", body["document_id"])
            body["revision"] = state.get("revision")
            return self._call_envelope(op, body, unwrap)

    def _call_envelope(self, op: str, body: dict, unwrap: str = "result") -> dict:
        status, parsed = self._request("POST", f"/{op}", body)
        return _unwrap(parsed, op, status, unwrap)

    def _document_id(self) -> int:
        return int(self.state().get("document_id", 1))

    # -- Leituras ----------------------------------------------------------

    def ready(self) -> dict:
        """``GET /ready`` — o servidor está no ar e pronto."""
        _status, parsed = self._request("GET", "/ready")
        if not parsed.get("ok"):
            raise OcsError(f"OCS não está ready: {parsed}", code="not_ready")
        return parsed

    def state(self) -> dict:
        """``GET /state`` — documento ativo, revision, capabilities."""
        _status, parsed = self._request("GET", "/state")
        return parsed

    def capabilities(self) -> dict:
        """``GET /capabilities`` — operações anunciadas pela build."""
        _status, parsed = self._request("GET", "/capabilities")
        return parsed

    def query_entities(
        self,
        *,
        layer: str | None = None,
        detail: str = "geometry",
        limit: int = 100,
    ) -> list[dict]:
        """``GET /entities`` — entidades do documento ativo."""
        query = f"?detail={detail}&limit={limit}"
        if layer is not None:
            query += f"&layer={urllib.request.quote(layer)}"
        _status, parsed = self._request("GET", f"/entities{query}")
        entities = parsed.get("entities", [])
        if not isinstance(entities, list):
            raise OcsError(f"resposta de /entities sem lista: {parsed}", code="bad_response")
        return entities

    def _stroke_entities(self, *, limit: int = ENTITY_SCAN_LIMIT) -> list[dict]:
        """Entidades de traço do Draw (dicts completos, com geometria).

        O filtro por ``layer`` vai na query (o OCS aceita ``?layer=``);
        o por ``space`` é do lado do cliente: a query também devolve
        geometria de definição de bloco (ex.: as setas
        ``_ARCHTICK``/``_CLOSEDBLANK``/``_CLOSEDFILLED`` do template
        DXF do ezdxf, em layer ``0``) — essas NÃO são traços.

        Uma resposta com exatamente ``limit`` itens pode estar
        truncada (a API não expõe offset): falha fechada, porque toda
        prova de presença/ausência deste módulo parte da varredura
        COMPLETA. Menos que o limite é prova de completude; mais, um
        servidor que ignorou o cap (completa do mesmo jeito).
        """
        entities = self.query_entities(layer=LAYER, detail="geometry", limit=limit)
        if len(entities) == limit:
            raise OcsError(
                f"/entities devolveu {len(entities)} entidades de traço "
                f"(= o limite pedido, {limit}): a lista pode estar "
                "truncada e a varredura não provaria completude",
                code="entities_truncated",
            )
        return [
            entity for entity in entities
            if entity.get("layer") == LAYER and entity.get("space") == "*Model_Space"
        ]

    def stroke_handles(self) -> list[str]:
        """Handles dos traços do Draw: layer ``DRAW_STROKES`` em ``*Model_Space``."""
        handles: list[str] = []
        for entity in self._stroke_entities():
            handle = entity.get("handle")
            if not isinstance(handle, str) or not handle:
                raise OcsError(
                    f"entidade de traço sem handle: {entity!r}",
                    code="bad_response",
                )
            handles.append(handle)
        return handles

    # -- Documentos ---------------------------------------------------------

    def open_dxf(self, dxf_path: str | Path) -> dict:
        """``POST /documents`` — abre um DXF como documento ativo."""
        _status, parsed = self._request(
            "POST", "/documents", {"path": str(dxf_path)}
        )
        if not parsed.get("ok", True):
            raise _error_from_body(parsed, _status)
        return parsed

    # -- XDATA (o id do traço) ----------------------------------------------

    def set_stroke_id(self, handle: str, stroke_id: str) -> dict:
        """Grava ``stroke_id`` em XDATA sob o APPID próprio ``DRAW``.

        Código DXF 1000 (string). Devolve o ``result`` do servidor
        (``{"app": "DRAW", "updated": 1}`` no spike).
        """
        _status, parsed = self._request(
            "PUT",
            f"/entities/{handle}/xdata/{APPID}",
            [{"code": 1000, "value": stroke_id}],
        )
        if not parsed.get("ok", False):
            raise _error_from_body(parsed, _status)
        return parsed.get("result", parsed)

    def get_stroke_id(
        self, handle: str, *, request_id: str | None = None
    ) -> str | None:
        """Lê o ``stroke_id`` de volta (op de envelope ``xdata_get``).

        A rota REST documentada (``GET /entities/{h}/xdata/{app}``)
        responde ``unknown_route`` na v2026.44; por isso a leitura usa
        o envelope. Devolve ``None`` quando não há XDATA ``DRAW``.
        """
        body = {
            "protocol": _PROTOCOL,
            "op": "xdata_get",
            "request_id": request_id or _new_request_id(),
            "document_id": self._document_id(),
            "handles": [handle],
            "app": APPID,
        }
        _status, parsed = self._request("POST", "/xdata_get", body)
        if not parsed.get("ok", False):
            raise _error_from_body(parsed, _status)
        items = parsed.get("items", [])
        if not isinstance(items, list):
            raise OcsError(
                f"xdata_get devolveu 'items' não-lista: {parsed}",
                code="bad_response",
            )
        for item in items:
            if isinstance(item, dict) and str(
                item.get("handle", "")
            ).upper() == handle.upper():
                xdata = item.get("xdata")
                if xdata is None:
                    return None
                if not isinstance(xdata, dict):
                    raise OcsError(
                        f"xdata_get devolveu xdata malformado para "
                        f"{handle}: {xdata!r}",
                        code="bad_response",
                    )
                values = xdata.get(APPID)
                if values is None:
                    return None
                if (
                    not isinstance(values, list)
                    or not values
                    or not isinstance(values[0], str)
                ):
                    raise OcsError(
                        f"xdata_get devolveu lista {APPID} malformada "
                        f"para {handle}: {values!r}",
                        code="bad_response",
                    )
                return values[0]
        return None

    # -- Apagar (borracha) ---------------------------------------------------

    def scan_stroke_ids(self) -> dict[str, str]:
        """Mapa ``stroke_id -> handle`` de TODOS os traços com XDATA ``DRAW``.

        Varredura completa: cada handle ``DRAW_STROKES``/``*Model_Space``
        tem seu XDATA lido de volta. É a leitura que prova ausência
        (um id apagado não aparece aqui) e presença (os demais mantêm
        seus ids). Handles sem XDATA ``DRAW`` não entram no mapa.
        """
        scanned: dict[str, str] = {}
        for handle in self.stroke_handles():
            stroke_id = self.get_stroke_id(handle)
            if stroke_id is None:
                continue
            if stroke_id in scanned:
                # Dois handles com o mesmo id = CAD corrompido: o dict
                # esconderia um deles e a prova rodaria incompleta.
                raise OcsError(
                    f"stroke_id duplicado no CAD: {stroke_id} "
                    f"(handles {scanned[stroke_id]} e {handle})",
                    code="duplicate_stroke_in_cad",
                )
            scanned[stroke_id] = handle
        return scanned

    def erase_strokes(
        self, stroke_ids: list[str], *, request_id: str | None = None
    ) -> dict:
        """Apaga do documento OCS as entidades com estes ``stroke_ids``.

        Via op de envelope ``entities_delete`` (API-SPEC v1.1 §6.3):
        tudo-ou-nada no servidor (``entity_absent`` se um handle não
        existir). O cliente ainda pré-confere, antes de qualquer
        mutação: cada id é resolvido para o handle pelo XDATA ``DRAW``
        e um id desconhecido falha fechada (``unknown_stroke_id``) sem
        apagar nada. Depois do delete, prova a ausência com uma
        varredura completa de XDATA e confere que os demais traços
        mantêm seus ids. Devolve o relatório do apagar.
        """
        ordered = list(dict.fromkeys(stroke_ids))
        if not ordered:
            raise OcsError(
                "erase_strokes sem stroke_ids: nada a apagar",
                code="empty_erase",
            )
        current = self.scan_stroke_ids()
        unknown = [sid for sid in ordered if sid not in current]
        if unknown:
            raise OcsError(
                f"não é possível apagar stroke_id desconhecido no CAD: "
                f"{unknown} (nada foi apagado)",
                code="unknown_stroke_id",
            )
        handles = [current[sid] for sid in ordered]
        result = self._envelope(
            "entities_delete",
            {"handles": handles},
            request_id=request_id,
        )
        erased = result.get("erased")
        if erased != len(handles):
            raise OcsError(
                f"entities_delete apagou {erased} de {len(handles)} "
                f"entidades pedidas: {ordered}",
                code="erase_mismatch",
            )
        after = self.scan_stroke_ids()
        still_there = [sid for sid in ordered if sid in after]
        if still_there:
            raise OcsError(
                f"apagar falhou: {still_there} continuam no CAD após "
                f"entities_delete",
                code="erase_failed",
            )
        return {
            "erased": ordered,
            "handles": handles,
            "erased_count": erased,
            "remaining": after,
            "verified_absent": True,
        }

    def sync_document(
        self,
        document: dict,
        *,
        expected_live: list[str] | None = None,
        out_path: str | Path | None = None,
    ) -> dict:
        """Re-sincroniza o CAD a partir do documento ``draw.paper.v1``.

        O documento é o mestre; o CAD segue o documento. Lê as
        tumbstones (``status == "erased"``) e deleta do CAD o que ainda
        está lá; prova a ausência dos apagados com uma varredura
        completa de XDATA e confere que todos os traços ``stable``
        estão presentes com seus ids (``pending`` nunca foi escrito
        no DXF da F0 — não se exige no CAD). Falha fechada, sem apagar nada, quando
        o CAD tem um traço vivo que o documento não tem
        (``stroke_missing_in_cad``) ou um id que o documento desconhece
        (``unknown_stroke_in_cad``) — divergência silenciosa não é
        resolvida adivinhando. Com ``out_path``, roda ``audit`` +
        ``save_verified`` ao fim (recusa se ``dropped_on_save > 0``).

        ``expected_live`` restringe os traços vivos que o CAD deve
        carregar: sem ele, todos os não-``erased`` são exigidos; com
        ele (ex.: o ``included_stroke_ids`` do escritor DXF, que omite
        traços de 1 ponto e ``pending``), um traço vivo fora da lista
        é registrado em ``live_not_in_cad`` em vez de falhar — o CAD
        nunca recebeu esse traço, não é divergência. Ids desconhecidos
        ou ``erased`` em ``expected_live`` são recusados.
        """
        if not isinstance(document, dict) or not isinstance(
            document.get("strokes"), list
        ):
            raise ValueError(
                "sync_document precisa de um documento draw.paper.v1 "
                "(dict com lista 'strokes')"
            )
        doc_ids: dict[str, str] = {}
        for stroke in document["strokes"]:
            sid = stroke.get("stroke_id") if isinstance(stroke, dict) else None
            status = stroke.get("status") if isinstance(stroke, dict) else None
            if not isinstance(sid, str) or not sid:
                raise ValueError(f"traço sem stroke_id válido: {stroke!r}")
            if status not in ("pending", "stable", "erased"):
                raise ValueError(
                    f"traço {sid} com status desconhecido: {status!r}"
                )
            if sid in doc_ids:
                # Um segundo traço com o mesmo id seria engolido pelo
                # dict e as provas rodariam sobre um mapa corrompido.
                raise ValueError(
                    f"documento com stroke_id duplicado: {sid!r}")
            doc_ids[sid] = status
        erased_ids = [sid for sid, st in doc_ids.items() if st == "erased"]
        if expected_live is None:
            # Sem projeção informada: stable-only é a melhor aproximação
            # do conjunto que o DXF da F0 carrega (pending e traços de
            # 1 ponto nunca entram no CAD).
            live_ids = [sid for sid, st in doc_ids.items() if st == "stable"]
        else:
            if not isinstance(expected_live, list) or any(
                not isinstance(sid, str) for sid in expected_live
            ):
                raise ValueError("expected_live deve ser uma lista de stroke_ids")
            if len(set(expected_live)) != len(expected_live):
                raise ValueError("expected_live com stroke_id duplicado")
            bad = [
                sid for sid in expected_live
                if doc_ids.get(sid) in (None, "erased")
            ]
            if bad:
                raise ValueError(
                    f"expected_live com ids que não são traços vivos do "
                    f"documento: {bad}"
                )
            live_ids = list(expected_live)
        current = self.scan_stroke_ids()
        unknown_in_cad = [sid for sid in current if sid not in doc_ids]
        if unknown_in_cad:
            raise OcsError(
                f"CAD tem stroke_ids que o documento desconhece: "
                f"{unknown_in_cad} (nada foi apagado)",
                code="unknown_stroke_in_cad",
            )
        missing_live = [sid for sid in live_ids if sid not in current]
        if missing_live:
            raise OcsError(
                f"CAD não tem traços vivos do documento: {missing_live} "
                f"(nada foi apagado; reabra o DXF com sync_strokes)",
                code="stroke_missing_in_cad",
            )
        to_erase = [sid for sid in erased_ids if sid in current]
        erased_now: list[str] = []
        if to_erase:
            report = self.erase_strokes(to_erase)
            erased_now = report["erased"]
        after = self.scan_stroke_ids()
        still_there = [sid for sid in erased_ids if sid in after]
        if still_there:
            raise OcsError(
                f"sync_document falhou: {still_there} continuam no CAD",
                code="erase_failed",
            )
        saved = None
        if out_path is not None:
            dropped = _audit_dropped(self.audit())
            if dropped:
                raise OcsError(
                    f"audit recusou o save: dropped_on_save={dropped}",
                    code="audit_lossy",
                )
            saved = self.save_verified(out_path)
        return {
            "erased": erased_now,
            "already_absent": [sid for sid in erased_ids if sid not in current],
            "live": live_ids,
            "live_not_in_cad": [
                sid for sid, st in doc_ids.items()
                if st != "erased" and sid not in live_ids
            ],
            "remaining": after,
            "verified_absent": True,
            "save_verified": saved,
        }

    # -- Auditoria e save verificado ----------------------------------------

    def audit(
        self,
        *,
        target_format: str = "dxf",
        target_version: str = "2000",
        request_id: str | None = None,
    ) -> dict:
        """``audit`` — relatório com ``dropped_on_save`` (alimenta G2.3)."""
        result = self._envelope(
            "audit",
            {"parameters": {"target_format": target_format,
                            "target_version": target_version}},
            request_id=request_id,
            unwrap="body",
        )
        return result

    def save_verified(
        self,
        path: str | Path,
        *,
        target_format: str = "dxf",
        target_version: str = "2000",
        overwrite: bool = False,
        request_id: str | None = None,
    ) -> dict:
        """``save_verified`` — escrita com verificação, sem perda silenciosa.

        Recusa (levanta ``OcsError``) quando ``verified`` não é true
        ou quando ``dropped_on_save > 0``: conversão com perda não
        passa em silêncio. Devolve o ``result`` (``verified``, ``sha256``,
        ``dropped_on_save``, ``manifest``).
        """
        result = self._envelope(
            "save_verified",
            {
                "path": str(path),
                "target_format": target_format,
                "target_version": target_version,
                "overwrite": overwrite,
            },
            request_id=request_id,
        )
        dropped = result.get("dropped_on_save", 0)
        if result.get("verified") is not True or dropped:
            raise OcsError(
                f"save_verified recusou {path}: verified={result.get('verified')}, "
                f"dropped_on_save={dropped}",
                code="save_failed",
            )
        return result

    # -- Passos em sequência (sem o `batch` do servidor) --------------------

    def run_steps(self, steps: list[dict]) -> dict:
        """Executa passos em sequência, um por requisição (≤ 64).

        Cada passo é ``{"op": ..., "params": {...}, "request_id": ...}``.
        Para no primeiro erro e relata ``completed_steps`` /
        ``next_step`` — a semântica do ``batch`` do API-SPEC, sem
        depender da rota ``/batch`` (ausente no REST da v2026.44).
        """
        if len(steps) > BATCH_MAX_STEPS:
            raise ValueError(
                f"batch com {len(steps)} passos excede o limite de "
                f"{BATCH_MAX_STEPS} do protocolo"
            )
        results: list[dict] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(
                step.get("op"), str
            ) or not step["op"]:
                raise ValueError(
                    f"passo {index} malformado (precisa de 'op' "
                    f"string não vazia): {step!r}"
                )
            try:
                results.append(
                    self._envelope(
                        step["op"],
                        step.get("params"),
                        request_id=step.get("request_id"),
                    )
                )
            except OcsError as exc:
                return {
                    "completed_steps": index,
                    "next_step": index,
                    "results": results,
                    "failed_step": index,
                    "error": str(exc),
                    "code": exc.code,
                }
        return {
            "completed_steps": len(steps),
            "next_step": None,
            "results": results,
        }

    # -- Fluxo F2 ponta a ponta ----------------------------------------------

    def sync_strokes(
        self,
        dxf_path: str | Path,
        stroke_ids: list[str],
        *,
        out_path: str | Path,
    ) -> dict:
        """DXF F0 → OCS → XDATA ``DRAW`` → ``audit`` → ``save_verified``.

        ``stroke_ids`` deve estar na ordem das entidades de traço do
        DXF (a ordem do documento ``draw.paper.v1``, que é a ordem em
        que ``export_dxf`` escreve). O pareamento handle ↔ stroke_id
        NÃO confia na ordem em que o OCS devolve as entidades: cada
        entidade é casada com o traço do DXF pelos VÉRTICES (lidos
        host-side com ezdxf). Um traço sem geometria correspondente no
        DXF, um vértice divergente ou um XDATA importado que discorde
        do id declarado falham fechados ANTES de qualquer escrita —
        um ``stroke_id`` na entidade errada é corrupção de
        proveniência, não um detalhe. Devolve um relatório com tempos
        medidos — ``open_to_queryable_s`` é o proxy do gate G2.1
        (≤ 2 s) — e com ``dropped_on_save`` (gate G2.3) e o SHA-256 do
        arquivo verificado. Nada é fingido: qualquer recusa levanta
        ``OcsError``.
        """
        if not stroke_ids or any(
            not isinstance(sid, str) or not sid for sid in stroke_ids
        ):
            raise ValueError(
                "sync_strokes precisa de stroke_ids (strings não vazias)")
        if len(set(stroke_ids)) != len(stroke_ids):
            raise ValueError("stroke_ids duplicados: cada id é único")
        expected = _dxf_stroke_vertices(dxf_path)
        if len(expected) != len(stroke_ids):
            raise OcsError(
                f"DXF tem {len(expected)} traços em {LAYER} mas foram "
                f"passados {len(stroke_ids)} stroke_ids",
                code="stroke_count_mismatch",
            )
        timings: dict[str, float] = {}
        started = time.perf_counter()
        self.open_dxf(dxf_path)
        timings["open_s"] = round(time.perf_counter() - started, 3)
        query_started = time.perf_counter()
        entities = self._stroke_entities()
        timings["open_to_queryable_s"] = round(time.perf_counter() - started, 3)
        timings["query_s"] = round(time.perf_counter() - query_started, 3)
        if len(entities) != len(expected):
            raise OcsError(
                f"CAD abriu {len(entities)} traços em {LAYER} mas o DXF "
                f"tem {len(expected)}",
                code="stroke_count_mismatch",
            )
        # Pareamento por GEOMETRIA, nunca pela ordem da resposta do
        # OCS: cada entidade casa com UM traço do DXF (o índice i leva
        # stroke_ids[i], por contrato com o chamador).
        handle_of_index = self._pair_entities(entities, expected)
        handles = [handle_of_index[i] for i in range(len(stroke_ids))]
        # Antes de escrever: se o OCS importou o XDATA que o próprio
        # DXF já carrega (F0 grava stroke_id por entidade), ele tem de
        # concordar com o pareamento — uma divergência significa
        # stroke_ids declarados em ordem errada ou arquivo adulterado.
        for index, stroke_id in enumerate(stroke_ids):
            handle = handle_of_index[index]
            existing = self.get_stroke_id(handle)
            if existing is not None and existing != stroke_id:
                raise OcsError(
                    f"entidade {handle} já traz XDATA {existing!r} do "
                    f"DXF, mas foi declarada {stroke_id!r}: nada foi "
                    "escrito",
                    code="xdata_conflict",
                )
        round_trip: dict[str, str | None] = {}
        for index, stroke_id in enumerate(stroke_ids):
            handle = handle_of_index[index]
            self.set_stroke_id(handle, stroke_id)
            round_trip[stroke_id] = self.get_stroke_id(handle)
        mismatched = {
            sid: got for sid, got in round_trip.items() if got != sid
        }
        if mismatched:
            raise OcsError(
                f"xdata round-trip divergiu: {mismatched}",
                code="xdata_mismatch",
            )
        dropped = _audit_dropped(self.audit())
        if dropped:
            raise OcsError(
                f"audit recusou o save: dropped_on_save={dropped}",
                code="audit_lossy",
            )
        saved = self.save_verified(out_path)
        return {
            "dxf_path": str(dxf_path),
            "out_path": str(out_path),
            "handles": handles,
            "stroke_ids": list(stroke_ids),
            "xdata_round_trip": round_trip,
            "timings_s": timings,
            "gate_g2_1_open_to_queryable_s": timings["open_to_queryable_s"],
            "dropped_on_save": dropped,
            "save_verified": {
                "verified": saved.get("verified"),
                "sha256": saved.get("sha256"),
                "dropped_on_save": saved.get("dropped_on_save"),
            },
        }

    @staticmethod
    def _pair_entities(
        entities: list[dict], expected: list[list[tuple[float, float]]]
    ) -> dict[int, str]:
        """índice do traço do DXF -> handle OCS, por vértices.

        Cada entidade OCS casa com exatamente um traço ainda não
        consumido; formas idênticas são indistinguíveis por definição
        e consomem os índices na ordem do arquivo. Falha fechada em
        handle ausente/duplicado, entidade sem vértices legíveis ou
        geometria sem par no DXF.
        """
        remaining = list(range(len(expected)))
        handle_of_index: dict[int, str] = {}
        for entity in entities:
            handle = entity.get("handle")
            if not isinstance(handle, str) or not handle:
                raise OcsError(
                    f"entidade de traço sem handle: {entity!r}",
                    code="bad_response",
                )
            if handle in handle_of_index.values():
                raise OcsError(
                    f"handle duplicado na resposta do OCS: {handle}",
                    code="bad_response",
                )
            vertices = _ocs_entity_vertices(entity)
            index = next(
                (
                    i for i in remaining
                    if vertices is not None
                    and _same_vertices(vertices, expected[i])
                ),
                None,
            )
            if index is None:
                raise OcsError(
                    f"entidade {handle} do CAD não corresponde por "
                    "geometria a nenhum traço do DXF (importação "
                    "alterou a forma ou o DXF diverge dos stroke_ids)",
                    code="geometry_mismatch",
                )
            remaining.remove(index)
            handle_of_index[index] = handle
        return handle_of_index


def _audit_dropped(audit_report: dict) -> Any:
    """``target.dropped_on_save`` do audit — a evidência do gate G2.3.

    Um ``target`` ausente ou malformado NÃO vale "sem perda": sem o
    campo não há prova de que o save seria lossless — falha fechada.
    """
    target = audit_report.get("target")
    if not isinstance(target, dict) or "dropped_on_save" not in target:
        raise OcsError(
            f"audit sem 'target.dropped_on_save' legível: {audit_report}",
            code="bad_response",
        )
    return target["dropped_on_save"]


def _open_ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Vértices sem o ponto final repetido (anel fechado guarda o flag,
    não o vértice duplicado — igual ao ``_ring`` do step_export)."""
    if len(points) > 1 and points[0] == points[-1]:
        return points[:-1]
    return points


def _dxf_entity_points(entity: Any) -> list[tuple[float, float]] | None:
    """Vértices (x, y) de uma entidade DXF de traço, ou ``None``.

    ``export_dxf`` escreve só LWPOLYLINE; LINE e POLYLINE legada são
    aceitas para DXFs feitos à mão. Tipos sem vértices legíveis não
    são traços pareáveis.
    """
    dxftype = entity.dxftype()
    if dxftype == "LWPOLYLINE":
        return [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
    if dxftype == "LINE":
        return [
            (float(entity.dxf.start.x), float(entity.dxf.start.y)),
            (float(entity.dxf.end.x), float(entity.dxf.end.y)),
        ]
    if dxftype == "POLYLINE":
        return [
            (float(v.dxf.location.x), float(v.dxf.location.y))
            for v in entity.vertices
        ]
    return None


def _dxf_stroke_vertices(dxf_path: str | Path) -> list[list[tuple[float, float]]]:
    """Vértices dos traços ``DRAW_STROKES`` do DXF, NA ORDEM DO ARQUIVO.

    O arquivo é a verdade do pareamento: a entidade i do arquivo leva
    ``stroke_ids[i]`` por contrato com o chamador, e cada entidade do
    CAD é casada a ela por geometria — nunca pela ordem da resposta
    do OCS. Import e leitura são host-side (ezdxf já é dependência do
    escritor DXF da F0; fica lazy para não pesar o import do cliente).
    """
    try:
        import ezdxf
    except ImportError as exc:
        raise OcsError(
            "pareamento por geometria precisa do ezdxf instalado",
            code="dxf_unreadable",
        ) from exc
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as exc:
        raise OcsError(
            f"DXF ilegível para o pareamento de traços: {dxf_path}: {exc}",
            code="dxf_unreadable",
        ) from exc
    shapes: list[list[tuple[float, float]]] = []
    for entity in doc.modelspace():
        if entity.dxf.get("layer") != LAYER:
            continue
        points = _dxf_entity_points(entity)
        if points is not None:
            shapes.append(_open_ring(points))
    return shapes


def _ocs_entity_vertices(entity: dict) -> list[tuple[float, float]] | None:
    """Vértices (x, y) de uma entidade OCS, ou ``None`` se ilegíveis."""
    vertices = entity.get("vertices")
    if not isinstance(vertices, list):
        return None
    points: list[tuple[float, float]] = []
    for vertex in vertices:
        if (
            not isinstance(vertex, (list, tuple))
            or len(vertex) < 2
            or any(
                isinstance(v, bool) or not isinstance(v, (int, float))
                for v in vertex[:2]
            )
        ):
            return None
        points.append((float(vertex[0]), float(vertex[1])))
    return _open_ring(points)


def _same_vertices(
    a: list[tuple[float, float]], b: list[tuple[float, float]]
) -> bool:
    """Mesma forma ponto a ponto, dentro de ``_GEOMETRY_TOLERANCE_MM``."""
    return len(a) == len(b) and all(
        abs(pa[0] - pb[0]) <= _GEOMETRY_TOLERANCE_MM
        and abs(pa[1] - pb[1]) <= _GEOMETRY_TOLERANCE_MM
        for pa, pb in zip(a, b)
    )


def _new_request_id() -> str:
    return f"ocre-{uuid.uuid4().hex[:24]}"


def _error_from_body(parsed: dict, http_status: int | None) -> OcsError:
    code = str(parsed.get("code") or "error")
    message = str(parsed.get("error") or parsed)
    if http_status == 401 or code == "unauthorized":
        return OcsError(f"OCS: não autorizado (Bearer inválido?): {message}",
                        code="unauthorized")
    if code == "stale_state":
        return OcsError(f"OCS: stale_state — atualizar o state e tentar de novo: {message}",
                        code="stale_state")
    return OcsError(f"OCS: {message}", code=code)


def _unwrap(parsed: dict, op: str, http_status: int | None, unwrap: str = "result") -> dict:
    """Extrai o conteúdo útil de uma resposta de envelope; levanta se ``ok`` é falso."""
    if not parsed.get("ok", False):
        raise _error_from_body(parsed, http_status)
    if unwrap == "body":
        return parsed
    result = parsed.get("result")
    if not isinstance(result, dict):
        raise OcsError(
            f"resposta de {op} sem result: {parsed}", code="bad_response"
        )
    return result
