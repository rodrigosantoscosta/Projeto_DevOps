#!/usr/bin/env python3
"""Validador deterministico de links Markdown (GFM) e diagramas Mermaid para GitHub.

Verifica em arquivos .md (especificacoes, planos, ADRs, READMEs e docs):
1. Links incompativeis com o GitHub: esquemas file://, caminhos absolutos de
   sistema de arquivos (/home/..., /Users/..., /mnt/c/..., C:\\...).
2. Links relativos quebrados ou escapando da raiz do repositorio (path traversal):
   verifica se arquivos/diretorios referenciados existem dentro do repositorio.
3. Ancoras GFM (#slug): valida se a secao referenciada existe no proprio
   arquivo ou no arquivo .md de destino (seguindo o algoritmo de slug do GitHub),
   ignorando headings comentados em HTML (<!-- ... -->) e permitindo ancoras
   de linha (#L10, #L10-L20) para arquivos de codigo.
4. Sintaxe Mermaid compativel com o GitHub:
   - Header de diagrama valido (flowchart, sequenceDiagram, stateDiagram-v2, etc.).
   - Labels de nos ou subgraphs sem aspas duplas contendo caracteres que quebram
     o parser do GitHub: parenteses (), colchetes [], chaves {}, dois-pontos :,
     barras / ou @.
   - Aspas duplas internas nao escapadas dentro de ["..."] (deve usar #quot; ou ').
   - Crases (`code`) ou links Markdown dentro de labels Mermaid.
   - Delimitadores de nos desbalanceados ou blocos Mermaid vazios.
   - Tags HTML nao suportadas fora de <br/> basico.
5. Regra Zero Emojis: detecta emojis unicode nos arquivos Markdown (fora de
   blocos de codigo e comentarios HTML).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

SUPPORTED_MERMAID_HEADERS = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram-v2",
    "stateDiagram",
    "classDiagram",
    "erDiagram",
    "gantt",
    "pie",
    "gitGraph",
    "mindmap",
    "timeline",
    "journey",
    "quadrantChart",
    "requirementDiagram",
    "C4Context",
    "C4Container",
    "C4Component",
    "C4Dynamic",
    "C4Deployment",
    "sankey-beta",
    "xychart-beta",
    "block-beta",
    "packet-beta",
    "kanban",
    "architecture-beta",
)

FRAGILE_GITHUB_HEADERS = {
    "xychart-beta",
    "block-beta",
    "architecture-beta",
    "packet-beta",
    "sankey-beta",
    "kanban",
}

EXTERNAL_SCHEMES = (
    "http://",
    "https://",
    "mailto:",
    "tel:",
    "data:",
)

FORBIDDEN_SCHEMES = (
    "file://",
    "vscode://",
    "cursor://",
    "conversation://",
)

WINDOWS_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")
UNIX_LOCAL_ABS_RE = re.compile(r"^/(home|Users|mnt|tmp|var|etc|opt|root|private)/")
LINE_ANCHOR_RE = re.compile(r"^L\d+(-L?\d+)?$")

FENCED_BLOCK_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
INLINE_CODE_RE = re.compile(r"`+[^`]*`+")
MD_LINK_RE = re.compile(
    r"(!?\[(?:[^\[\]]|\[[^\[\]]*\])*\])\(\s*(<[^>]+>|(?:[^()\s]|\([^()\s]*\))+)(?:\s+[\"'][^\"']*[\"'])?\s*\)"
)
REF_DEF_RE = re.compile(r"^\s*\[([^\]]+)\]:\s*(\S+)")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
HTML_ANCHOR_RE = re.compile(r"<a\s+[^>]*?(?:id|name)=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0"
    "]+",
    flags=re.UNICODE,
)


@dataclass
class Finding:
    severity: str  # "BLOCKING" | "WARNING"
    category: str  # "LINK" | "ANCHOR" | "MERMAID" | "EMOJI"
    file: str
    line: int
    message: str
    suggestion: str


def github_slugify(heading_text: str) -> str:
    """Reproduz o algoritmo de slug de headings do GitHub (github-slugger)."""
    text = re.sub(r"`([^`]*)`", r"\1", heading_text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.strip().lower()
    slug_chars: list[str] = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_"):
            slug_chars.append(ch)
        elif ch == " ":
            slug_chars.append("-")
    return "".join(slug_chars)


def _strip_html_comments_from_line(line: str, in_comment: bool) -> tuple[str, bool]:
    """Remove trechos dentro de comentarios HTML <!-- ... --> preservando trechos fora deles."""
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        if in_comment:
            end_idx = line.find("-->", i)
            if end_idx == -1:
                return "".join(out), True
            in_comment = False
            i = end_idx + 3
        else:
            start_idx = line.find("<!--", i)
            if start_idx == -1:
                out.append(line[i:])
                break
            out.append(line[i:start_idx])
            in_comment = True
            i = start_idx + 4
    return "".join(out), in_comment


def _strip_mermaid_inline_comment(stmt: str) -> str:
    """Remove comentario inline `%% ...` fora de aspas duplas em linhas Mermaid."""
    in_quotes = False
    i = 0
    n = len(stmt)
    while i < n:
        ch = stmt[i]
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes and stmt[i : i + 2] == "%%":
            return stmt[:i].strip()
        i += 1
    return stmt.strip()


def extract_headings_and_blocks(md_path: Path) -> tuple[set[str], list[tuple[int, str]], list[tuple[int, str]]]:
    """Retorna (conjunto de slugs GFM, linhas fora de codeblocks/comentarios, blocos mermaid)."""
    content = md_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    slug_counts: dict[str, int] = {}
    slugs: set[str] = set()
    non_code_lines: list[tuple[int, str]] = []
    mermaid_blocks: list[tuple[int, str]] = []

    in_fence = False
    fence_marker = ""
    is_mermaid = False
    mermaid_start_line = 0
    mermaid_buf: list[str] = []
    in_html_comment = False

    for idx, raw_line in enumerate(lines, start=1):
        fence_match = FENCED_BLOCK_RE.match(raw_line)
        if not in_fence:
            if not in_html_comment and fence_match:
                in_fence = True
                fence_marker = fence_match.group(2)
                info_str = fence_match.group(3).strip().lower()
                is_mermaid = info_str == "mermaid" or info_str.startswith("mermaid ")
                if is_mermaid:
                    mermaid_start_line = idx
                    mermaid_buf = []
                continue

            visible_line, in_html_comment = _strip_html_comments_from_line(raw_line, in_html_comment)
            if not visible_line.strip():
                continue

            non_code_lines.append((idx, visible_line))
            for anchor_match in HTML_ANCHOR_RE.finditer(visible_line):
                slugs.add(anchor_match.group(1).strip().lower())

            h_match = HEADING_RE.match(visible_line)
            if h_match:
                base_slug = github_slugify(h_match.group(2))
                if base_slug:
                    count = slug_counts.get(base_slug, 0)
                    slug_counts[base_slug] = count + 1
                    final_slug = base_slug if count == 0 else f"{base_slug}-{count}"
                    slugs.add(final_slug)
        else:
            if fence_match and fence_match.group(2).startswith(fence_marker[0]) and len(fence_match.group(2)) >= len(fence_marker) and not fence_match.group(3).strip():
                if is_mermaid:
                    mermaid_blocks.append((mermaid_start_line, "\n".join(mermaid_buf)))
                in_fence = False
                fence_marker = ""
                is_mermaid = False
                mermaid_buf = []
            elif is_mermaid:
                mermaid_buf.append(raw_line)

    if in_fence and is_mermaid:
        mermaid_blocks.append((mermaid_start_line, "\n".join(mermaid_buf)))

    return slugs, non_code_lines, mermaid_blocks


def validate_mermaid_block(
    file_str: str,
    start_line: int,
    block_content: str,
) -> list[Finding]:
    findings: list[Finding] = []
    raw_lines = block_content.splitlines()

    meaningful_lines: list[tuple[int, str]] = []
    for offset, line in enumerate(raw_lines, start=1):
        stripped = _strip_mermaid_inline_comment(line.strip())
        if not stripped:
            continue
        meaningful_lines.append((start_line + offset, stripped))

    if not meaningful_lines:
        findings.append(
            Finding(
                severity="BLOCKING",
                category="MERMAID",
                file=file_str,
                line=start_line,
                message="Bloco ```mermaid vazio ou sem instrucoes validas.",
                suggestion="Adicione um diagrama valido (ex.: flowchart TD) ou remova o bloco vazio.",
            )
        )
        return findings

    first_line_no, first_line = meaningful_lines[0]
    while first_line.startswith("---") and len(meaningful_lines) > 1:
        end_fm_idx = None
        for idx in range(1, len(meaningful_lines)):
            if meaningful_lines[idx][1].startswith("---"):
                end_fm_idx = idx
                break
        if end_fm_idx is not None and end_fm_idx + 1 < len(meaningful_lines):
            meaningful_lines = meaningful_lines[end_fm_idx + 1 :]
            first_line_no, first_line = meaningful_lines[0]
        else:
            break

    header_token = first_line.split()[0].rstrip(";")
    if not any(first_line.startswith(h) for h in SUPPORTED_MERMAID_HEADERS):
        findings.append(
            Finding(
                severity="BLOCKING",
                category="MERMAID",
                file=file_str,
                line=first_line_no,
                message=f"Tipo de diagrama Mermaid desconhecido ou invalido no GitHub: '{header_token}'.",
                suggestion="Inicie o bloco com flowchart TD, flowchart LR, sequenceDiagram, stateDiagram-v2, classDiagram ou erDiagram.",
            )
        )
        return findings

    if header_token in FRAGILE_GITHUB_HEADERS:
        findings.append(
            Finding(
                severity="WARNING",
                category="MERMAID",
                file=file_str,
                line=first_line_no,
                message=f"Diagrama '{header_token}' e experimental e frequentemente falha no renderizador nativo do GitHub.",
                suggestion="Prefira flowchart TD/LR, sequenceDiagram, stateDiagram-v2, classDiagram ou erDiagram para compatibilidade garantida no GitHub.",
            )
        )

    is_flowchart_like = header_token in ("flowchart", "graph")

    subgraph_unquoted_re = re.compile(r"^subgraph\s+[A-Za-z0-9_.-]+\s+\[(?!\")([^\]]*[\(\)\{\}:/][^\]]*)\]\s*$")
    subgraph_bare_parens_re = re.compile(r"^subgraph\s+(?![A-Za-z0-9_.-]+\s*\[)(?!\")(.+[\(\)\{\}:].+)$")

    for line_no, stmt in meaningful_lines[1:]:
        if is_flowchart_like:
            if subgraph_unquoted_re.match(stmt) or subgraph_bare_parens_re.match(stmt):
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="MERMAID",
                        file=file_str,
                        line=line_no,
                        message=f"Titulo de subgraph sem aspas contendo caracteres especiais: `{stmt}`.",
                        suggestion='Use ID explicito e titulo entre aspas duplas: `subgraph ID ["Titulo (Detalhe)"]`.',
                    )
                )

            _check_flowchart_line_nodes(file_str, line_no, stmt, findings)

        if "<script" in stmt.lower() or "<div" in stmt.lower() or "<span" in stmt.lower():
            findings.append(
                Finding(
                    severity="BLOCKING",
                    category="MERMAID",
                    file=file_str,
                    line=line_no,
                    message=f"Tag HTML nao suportada pelo Mermaid do GitHub na linha: `{stmt}`.",
                    suggestion="Remova tags HTML (<div, <span, <script); use apenas texto puro ou <br/> dentro de labels entre aspas.",
                )
            )

    return findings


def _check_flowchart_line_nodes(
    file_str: str,
    line_no: int,
    stmt: str,
    findings: list[Finding],
) -> None:
    """Inspeciona definicoes de nos em linhas de flowchart/graph para detectar sintaxe que quebra no GitHub."""
    stmt = _strip_mermaid_inline_comment(stmt)
    if not stmt or stmt.startswith(("classDef ", "class ", "style ", "linkStyle ", "click ", "direction ", "end")):
        return

    edge_label_re = re.compile(r"\|([^|]*)\|")
    for edge_match in edge_label_re.finditer(stmt):
        edge_text = edge_match.group(1)
        if any(ch in edge_text for ch in ("(", ")", "[", "]", "{", "}")):
            if not (edge_text.strip().startswith('"') and edge_text.strip().endswith('"')):
                findings.append(
                    Finding(
                        severity="WARNING",
                        category="MERMAID",
                        file=file_str,
                        line=line_no,
                        message=f"Label de seta `|{edge_text}|` contem parenteses/colchetes/chaves que podem quebrar o parser do GitHub.",
                        suggestion='Simplifique o texto da seta ou envolva em aspas: `|"texto"|`.',
                    )
                )

    stmt_no_edges = edge_label_re.sub("||", stmt)

    node_start_re = re.compile(r"\b([A-Za-z0-9_.-]+)\s*(\[\"|\[|\(\(|\(\[|\(|\{\{|\{)")
    pos = 0
    n = len(stmt_no_edges)

    while pos < n:
        m = node_start_re.search(stmt_no_edges, pos)
        if not m:
            break
        node_id = m.group(1)
        if node_id in ("subgraph", "classDef", "class", "style", "linkStyle", "click", "direction"):
            pos = m.end()
            continue

        open_tok = m.group(2)
        content_start = m.end()

        if open_tok == '["':
            close_idx = stmt_no_edges.find('"]', content_start)
            if close_idx == -1:
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="MERMAID",
                        file=file_str,
                        line=line_no,
                        message=f"No `{node_id}[\"...` nao possui fechamento `\"]` na mesma linha.",
                        suggestion='Feche o label na mesma linha usando `"]` e use `<br/>` para quebras de linha.',
                    )
                )
                break
            inner = stmt_no_edges[content_start:close_idx]
            if '"' in inner:
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="MERMAID",
                        file=file_str,
                        line=line_no,
                        message=f'Aspas duplas internas nao escapadas dentro do no `{node_id}["{inner}"]`.',
                        suggestion="Substitua aspas duplas internas por aspas simples (') ou `#quot;`.",
                    )
                )
            if "`" in inner:
                findings.append(
                    Finding(
                        severity="WARNING",
                        category="MERMAID",
                        file=file_str,
                        line=line_no,
                        message=f'Crases (`code`) dentro do no `{node_id}["{inner}"]` podem falhar no renderizador do GitHub.',
                        suggestion="Remova as crases de dentro do label Mermaid ou use aspas simples.",
                    )
                )
            pos = close_idx + 2
            continue

        close_map = {
            "[": "]",
            "(": ")",
            "((": "))",
            "([": "])",
            "{": "}",
            "{{": "}}",
        }
        close_tok = close_map[open_tok]
        rest = stmt_no_edges[content_start:]
        if rest.startswith('"'):
            end_quote_tok = '"' + close_tok
            close_idx = stmt_no_edges.find(end_quote_tok, content_start + 1)
            if close_idx == -1:
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="MERMAID",
                        file=file_str,
                        line=line_no,
                        message=f"No `{node_id}{open_tok}\"...` com delimitador `{end_quote_tok}` ausente ou desbalanceado.",
                        suggestion=f"Feche corretamente com `{end_quote_tok}`.",
                    )
                )
                break
            inner = stmt_no_edges[content_start + 1 : close_idx]
            if '"' in inner:
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="MERMAID",
                        file=file_str,
                        line=line_no,
                        message=f"Aspas duplas internas nao escapadas no no `{node_id}`.",
                        suggestion="Use aspas simples (') ou `#quot;` dentro do label.",
                    )
                )
            pos = close_idx + len(end_quote_tok)
            continue

        arrow_match = re.search(r"\s*(?:--+>|=+>|-\.+->|--+|==+)\s*", stmt_no_edges[content_start:])
        segment = stmt_no_edges[content_start : content_start + arrow_match.start()] if arrow_match else stmt_no_edges[content_start:]
        close_rel = segment.rfind(close_tok)
        if close_rel == -1:
            findings.append(
                Finding(
                    severity="BLOCKING",
                    category="MERMAID",
                    file=file_str,
                    line=line_no,
                    message=f"Delimitador `{close_tok}` nao encontrado para o no `{node_id}{open_tok}...`.",
                    suggestion=f'Envolva o texto do no em aspas duplas: `{node_id}{open_tok}"Texto"{close_tok}`.',
                )
            )
            break

        inner_unquoted = segment[:close_rel]
        if open_tok == "[" and inner_unquoted.startswith("(") and inner_unquoted.endswith(")"):
            inner_unquoted = inner_unquoted[1:-1]
        elif open_tok == "[" and inner_unquoted.startswith("[") and inner_unquoted.endswith("]"):
            inner_unquoted = inner_unquoted[1:-1]
        elif open_tok == "[" and inner_unquoted.startswith("/") and (inner_unquoted.endswith("/") or inner_unquoted.endswith("\\")):
            inner_unquoted = inner_unquoted[1:-1]

        bad_chars = [ch for ch in ("(", ")", "[", "]", "{", "}", '"', "`") if ch in inner_unquoted]
        if bad_chars:
            uniq_bad = "".join(sorted(set(bad_chars)))
            findings.append(
                Finding(
                    severity="BLOCKING",
                    category="MERMAID",
                    file=file_str,
                    line=line_no,
                    message=f"Label sem aspas no no `{node_id}` contem caracteres `{uniq_bad}` que quebram o Mermaid no GitHub: `{inner_unquoted}`.",
                    suggestion=f'Envolva o label entre aspas duplas: `{node_id}{open_tok}"{inner_unquoted.replace(chr(34), chr(39))}"{close_tok}`.',
                )
            )
        pos = content_start + close_rel + len(close_tok)


def validate_markdown_file(
    md_path: Path,
    repo_root: Path,
    slug_cache: dict[Path, set[str]],
    check_emojis: bool = True,
) -> list[Finding]:
    findings: list[Finding] = []
    md_abs = md_path.resolve()
    repo_root_abs = repo_root.resolve()
    try:
        rel_display = str(md_abs.relative_to(repo_root_abs))
    except ValueError:
        rel_display = str(md_path)

    slugs, non_code_lines, mermaid_blocks = extract_headings_and_blocks(md_abs)
    slug_cache[md_abs] = slugs

    targets_to_check: list[tuple[int, str]] = []
    for line_no, raw_line in non_code_lines:
        if check_emojis:
            emoji_match = EMOJI_RE.search(raw_line)
            if emoji_match:
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="EMOJI",
                        file=rel_display,
                        line=line_no,
                        message=f"Emoji unicode detectado ('{emoji_match.group(0)}') violando a Regra Zero Emojis.",
                        suggestion="Substitua por tags em texto puro ([OK], [NOTA], [AVISO], [CRITICO], [FALHA]).",
                    )
                )

        clean_line = INLINE_CODE_RE.sub("", raw_line)
        for m in MD_LINK_RE.finditer(clean_line):
            targets_to_check.append((line_no, m.group(2).strip()))

        ref_m = REF_DEF_RE.match(clean_line)
        if ref_m:
            targets_to_check.append((line_no, ref_m.group(2).strip()))

    for line_no, raw_target in targets_to_check:
        target = raw_target.strip("<>")
        if not target:
            continue

        low_target = target.lower()
        if any(low_target.startswith(s) for s in FORBIDDEN_SCHEMES):
            findings.append(
                Finding(
                    severity="BLOCKING",
                    category="LINK",
                    file=rel_display,
                    line=line_no,
                    message=f"Link com esquema local `{target}` fica quebrado no GitHub.",
                    suggestion="Em arquivos .md do repositorio, use caminhos relativos (ex.: `./src/arquivo.ts` ou `../docs/spec.md`).",
                )
            )
            continue

        if any(low_target.startswith(s) for s in EXTERNAL_SCHEMES):
            continue

        if WINDOWS_ABS_RE.match(target) or UNIX_LOCAL_ABS_RE.match(target):
            findings.append(
                Finding(
                    severity="BLOCKING",
                    category="LINK",
                    file=rel_display,
                    line=line_no,
                    message=f"Caminho absoluto de sistema de arquivos `{target}` fica quebrado no GitHub.",
                    suggestion="Converta para caminho relativo ao arquivo Markdown ou a raiz do repositorio.",
                )
            )
            continue

        if "#" in target:
            path_part, anchor_part = target.split("#", 1)
        else:
            path_part, anchor_part = target, ""

        path_part = urllib.parse.unquote(path_part)
        anchor_part = urllib.parse.unquote(anchor_part).strip().lower()

        if not path_part:
            if anchor_part and anchor_part not in slugs and not LINE_ANCHOR_RE.match(target.split("#", 1)[1]):
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="ANCHOR",
                        file=rel_display,
                        line=line_no,
                        message=f"Ancora interna `#{anchor_part}` nao corresponde a nenhum titulo neste documento.",
                        suggestion=f"Verifique a grafia do titulo ou use um dos slugs validos (ex.: {', '.join(sorted(list(slugs))[:4]) or 'nenhum'}).",
                    )
                )
            continue

        if "{" in path_part or "}" in path_part or "$" in path_part or "*" in path_part:
            continue

        if path_part.startswith("/"):
            resolved = (repo_root_abs / path_part.lstrip("/")).resolve()
        else:
            resolved = (md_abs.parent / path_part).resolve()

        # Verifica se o arquivo .md origem esta dentro de repo_root_abs; se estiver, bloqueia path traversal fora de repo_root_abs
        try:
            md_abs.relative_to(repo_root_abs)
            md_inside_repo = True
        except ValueError:
            md_inside_repo = False

        if md_inside_repo:
            try:
                resolved.relative_to(repo_root_abs)
            except ValueError:
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="LINK",
                        file=rel_display,
                        line=line_no,
                        message=f"Link relativo `{raw_target}` escapa da raiz do repositorio (`{repo_root_abs}`) via path traversal.",
                        suggestion="Mantenha todos os links relativos dentro dos limites da raiz do repositorio para que funcionem no GitHub.",
                    )
                )
                continue

        if not resolved.exists():
            findings.append(
                Finding(
                    severity="BLOCKING",
                    category="LINK",
                    file=rel_display,
                    line=line_no,
                    message=f"Link relativo quebrado `{raw_target}` -> arquivo ou pasta nao encontrado em `{resolved}`.",
                    suggestion="Corrija o caminho relativo (verifique niveis `../` e nome do arquivo).",
                )
            )
            continue

        if anchor_part and resolved.is_file() and resolved.suffix.lower() == ".md":
            if resolved not in slug_cache:
                target_slugs, _, _ = extract_headings_and_blocks(resolved)
                slug_cache[resolved] = target_slugs
            target_slugs = slug_cache[resolved]
            if anchor_part not in target_slugs and not LINE_ANCHOR_RE.match(target.split("#", 1)[1]):
                findings.append(
                    Finding(
                        severity="BLOCKING",
                        category="ANCHOR",
                        file=rel_display,
                        line=line_no,
                        message=f"Ancora `#{anchor_part}` nao existe no arquivo de destino `{path_part}`.",
                        suggestion="Atualize o fragmento `#...` para corresponder a um heading real do documento de destino.",
                    )
                )

    for start_line, block_content in mermaid_blocks:
        findings.extend(validate_mermaid_block(rel_display, start_line, block_content))

    return findings


def detect_git_repo_root(start_dir: Path) -> Path:
    """Descobre a raiz do repositorio Git via `git rev-parse --show-toplevel`, ou retorna start_dir."""
    probe_dir = start_dir if start_dir.is_dir() else start_dir.parent
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=probe_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except OSError:
        pass
    return probe_dir.resolve()


def discover_git_modified_markdown(repo_root: Path, base_ref: str | None = None) -> list[Path]:
    """Descobre arquivos .md modificados/adicionados no git status ou git diff."""
    found: set[Path] = set()
    git_base = ["git", "-c", "core.quotepath=false"]
    cmds = [
        [*git_base, "diff", "--name-only", "--diff-filter=ACMR"],
        [*git_base, "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        [*git_base, "ls-files", "--others", "--exclude-standard"],
    ]
    if base_ref:
        cmds.append([*git_base, "diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}...HEAD"])

    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    p = (repo_root / line.strip()).resolve()
                    if p.is_file() and p.suffix.lower() == ".md":
                        found.add(p)
        except OSError:
            pass
    return sorted(found)


def collect_markdown_files(paths: Iterable[str], repo_root: Path) -> list[Path]:
    ignored_dirs = {".git", "node_modules", ".venv", "venv", "dist", "build", ".next", "coverage", ".worktrees"}
    result: list[Path] = []
    seen: set[Path] = set()

    for raw in paths:
        p = Path(raw).resolve()
        if p.is_file() and p.suffix.lower() == ".md":
            if p not in seen:
                seen.add(p)
                result.append(p)
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in ignored_dirs]
                for f in sorted(files):
                    if f.lower().endswith(".md"):
                        fp = (Path(root) / f).resolve()
                        if fp not in seen:
                            seen.add(fp)
                            result.append(fp)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Valida links Markdown (GFM), ancoras e diagramas Mermaid para compatibilidade com o GitHub."
    )
    parser.add_argument("paths", nargs="*", help="Arquivos ou diretorios .md para validar (padrao: diretorio atual).")
    parser.add_argument("--git-diff", action="store_true", help="Valida apenas arquivos .md modificados no git.")
    parser.add_argument("--base-ref", default=None, help="Ref base opcional para --git-diff (ex.: origin/main).")
    parser.add_argument("--repo-root", default=None, help="Raiz do repositorio para resolver links absolutos '/...'.")
    parser.add_argument("--no-emoji-check", action="store_true", help="Desativa a verificacao da Regra Zero Emojis.")
    parser.add_argument("--json", action="store_true", help="Emite resultado estruturado em JSON.")
    args = parser.parse_args(argv)

    if args.repo_root:
        repo_root = Path(args.repo_root).resolve()
    elif args.paths:
        repo_root = detect_git_repo_root(Path(args.paths[0]).resolve())
    else:
        repo_root = detect_git_repo_root(Path.cwd().resolve())

    if args.git_diff:
        md_files = discover_git_modified_markdown(repo_root, args.base_ref)
        if not md_files:
            if args.json:
                print(json.dumps({"files_checked": 0, "blocking": 0, "warnings": 0, "findings": []}, indent=2))
            else:
                print("[OK] Nenhum arquivo Markdown (.md) modificado no git diff.")
            return 0
    else:
        target_paths = args.paths if args.paths else [str(repo_root)]
        md_files = collect_markdown_files(target_paths, repo_root)

    slug_cache: dict[Path, set[str]] = {}
    all_findings: list[Finding] = []
    for md_file in md_files:
        all_findings.extend(
            validate_markdown_file(
                md_file,
                repo_root=repo_root,
                slug_cache=slug_cache,
                check_emojis=not args.no_emoji_check,
            )
        )

    blocking = [f for f in all_findings if f.severity == "BLOCKING"]
    warnings = [f for f in all_findings if f.severity == "WARNING"]

    if args.json:
        print(
            json.dumps(
                {
                    "files_checked": len(md_files),
                    "blocking": len(blocking),
                    "warnings": len(warnings),
                    "findings": [asdict(f) for f in all_findings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for f in all_findings:
            tag = "[FALHA] Blocking" if f.severity == "BLOCKING" else "[AVISO] Warning"
            print(f"{tag} [{f.category}]: {f.file}:{f.line} -> {f.message}")
            print(f"  Correcao: {f.suggestion}")
        status_tag = "[OK]" if not blocking else "[FALHA]"
        print(
            f"{status_tag} Validacao GitHub Docs & Mermaid: "
            f"{len(md_files)} arquivo(s) verificado(s) | "
            f"Blocking: {len(blocking)} | Warnings: {len(warnings)}"
        )

    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
