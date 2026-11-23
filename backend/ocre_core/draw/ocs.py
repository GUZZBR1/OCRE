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


class OcsError(RuntimeError):
    """Erro do OpenCADStudio (resposta do servidor ou recusa local).

    ``code`` é o código simbólico do OCS (ex.: ``stale_state``,
    ``entity_absent``, ``save_failed``) ou um código local
    (``unavailable``, ``no_token``, ``bad_token_file``,
    ``not_loopback``). ``http_status`` é o HTTP, quando houve.
    """

    def __init__(self, message: str, *, code: str = "error", http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


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
    path = Path(token_file or os.environ.get("OCS_TOKEN_FILE", "") or "")
    if token_file is None and not os.environ.get("OCS_TOKEN_FILE"):
        raise OcsError(
            "sem token do OpenCADStudio: defina OCS_API_TOKEN, "
            "OCS_TOKEN_FILE (arquivo 0600) ou passe token=",
            code="no_token",
        )
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
                return resp.status, (json.loads(raw) if raw else {})
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

    def stroke_handles(self) -> list[str]:
        """Handles dos traços do Draw: layer ``DRAW_STROKES`` em ``*Model_Space``.

        O filtro por ``space`` é do lado do cliente: a query do OCS
        também devolve geometria de definição de bloco (ex.: as setas
        ``_ARCHTICK``/``_CLOSEDBLANK``/``_CLOSEDFILLED`` do template
        DXF do ezdxf, em layer ``0``) — essas NÃO são traços.
        """
        return [
            str(entity["handle"])
            for entity in self.query_entities(detail="geometry")
            if entity.get("layer") == LAYER and entity.get("space") == "*Model_Space"
        ]

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
        for item in items:
            if str(item.get("handle", "")).upper() == handle.upper():
                values = (item.get("xdata") or {}).get(APPID) or []
                return str(values[0]) if values else None
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
            if stroke_id is not None:
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
        out_path: str | Path | None = None,
    ) -> dict:
        """Re-sincroniza o CAD a partir do documento ``draw.paper.v1``.

        O documento é o mestre; o CAD segue o documento. Lê as
        tumbstones (``status == "erased"``) e deleta do CAD o que ainda
        está lá; prova a ausência dos apagados com uma varredura
        completa de XDATA e confere que todos os traços vivos estão
        presentes com seus ids. Falha fechada, sem apagar nada, quando
        o CAD tem um traço vivo que o documento não tem
        (``stroke_missing_in_cad``) ou um id que o documento desconhece
        (``unknown_stroke_in_cad``) — divergência silenciosa não é
        resolvida adivinhando. Com ``out_path``, roda ``audit`` +
        ``save_verified`` ao fim (recusa se ``dropped_on_save > 0``).
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
            doc_ids[sid] = status
        erased_ids = [sid for sid, st in doc_ids.items() if st == "erased"]
        live_ids = [sid for sid, st in doc_ids.items() if st != "erased"]
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
            audit_report = self.audit()
            dropped = (audit_report.get("target") or {}).get("dropped_on_save")
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

        ``stroke_ids`` deve estar na ordem das entidades
        ``DRAW_STROKES``/``*Model_Space`` do DXF (a ordem do documento
        ``draw.paper.v1``). Devolve um relatório com tempos medidos —
        ``open_to_queryable_s`` é o proxy do gate G2.1 (≤ 2 s) — e com
        ``dropped_on_save`` (gate G2.3) e o SHA-256 do arquivo verificado.
        Nada é fingido: qualquer recusa levanta ``OcsError``.
        """
        timings: dict[str, float] = {}
        started = time.perf_counter()
        self.open_dxf(dxf_path)
        timings["open_s"] = round(time.perf_counter() - started, 3)
        query_started = time.perf_counter()
        handles = self.stroke_handles()
        timings["open_to_queryable_s"] = round(time.perf_counter() - started, 3)
        timings["query_s"] = round(time.perf_counter() - query_started, 3)
        if len(handles) != len(stroke_ids):
            raise OcsError(
                f"DXF tem {len(handles)} traços em {LAYER} mas foram "
                f"passados {len(stroke_ids)} stroke_ids",
                code="stroke_count_mismatch",
            )
        round_trip: dict[str, str | None] = {}
        for handle, stroke_id in zip(handles, stroke_ids):
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
        audit_report = self.audit()
        target = (audit_report.get("target") or {})
        dropped = target.get("dropped_on_save")
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
            "stroke_ids": stroke_ids,
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


def _new_request_id() -> str:
    return f"ocre-{uuid.uuid4().hex[:24]}"


def _error_from_body(parsed: dict, http_status: int | None) -> OcsError:
    code = str(parsed.get("code") or "error")
    message = str(parsed.get("error") or parsed)
    if http_status == 401 or code == "unauthorized":
        return OcsError(f"OCS: não autorizado (Bearer inválido?): {message}",
                        code="unauthorized", http_status=http_status)
    if code == "stale_state":
        return OcsError(f"OCS: stale_state — atualizar o state e tentar de novo: {message}",
                        code="stale_state", http_status=http_status)
    return OcsError(f"OCS: {message}", code=code, http_status=http_status)


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
