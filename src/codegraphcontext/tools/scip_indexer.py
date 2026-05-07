"""
scip_indexer.py
---------------
SCIP-based indexing pipeline. Only activated when SCIP_INDEXER=true in config.

SCIP (Semantic Code Intelligence Protocol) is a language-agnostic protocol that
uses actual compiler / type-checker tooling (e.g. Pyright for Python, tsc for
TypeScript) to produce a single `index.scip` protobuf file containing:
  - Every symbol definition (function, class, variable) with its file + line
  - Every symbol reference, mapping back to its definition
  - Type signatures, docstrings, and symbol kinds

This gives us compiler-level accuracy for CALLS and INHERITS edges instead of
the heuristic imports_map approach used in Tree-sitter mode.

Workflow (called by GraphBuilder.build_graph_from_path_async when enabled):
  1. ScipIndexer.run(path) → runs the appropriate scip-<lang> CLI, returns path to index.scip
  2. ScipIndexParser.parse(index_scip_path) → returns {nodes, edges} dicts
  3. indexing.persistence.GraphWriter writes nodes + edges via the same Cypher MERGE queries as Tree-sitter mode.
  4. Tree-sitter supplement pass adds: cyclomatic_complexity, source text, decorators.

Supported SCIP indexers and their install commands:
  python     → pip install scip-python   (uses Pyright)
  typescript → npm install -g @sourcegraph/scip-typescript
  javascript → npm install -g @sourcegraph/scip-typescript  (same binary, uses --infer-tsconfig for JS-only projects)
  go         → go install github.com/sourcegraph/scip-go/cmd/scip-go@latest
  rust       → cargo install scip-rust (or rustup component add rust-analyzer)
  java       → https://github.com/sourcegraph/scip-java

JavaScript indexing notes:
  - Pure JS projects (no tsconfig.json): scip-typescript index --infer-tsconfig
  - Mixed JS/TS projects (tsconfig.json present): scip-typescript index  (tsconfig covers .js via allowJs)
  - Add @types/* packages as devDependencies for better type inference quality.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..utils.debug_log import error_logger, info_logger, warning_logger

# ---------------------------------------------------------------------------
# SCIP indexer orchestration
# ---------------------------------------------------------------------------

# Maps file extension → (language name, scip CLI binary name, install hint, docker image)
EXTENSION_TO_SCIP: Dict[str, Tuple[str, str, str, str]] = {
    ".py":   ("python",     "scip-python",     "pip install scip-python", "sourcegraph/scip-python"),
    ".ipynb":("python",     "scip-python",     "pip install scip-python", "sourcegraph/scip-python"),
    ".ts":   ("typescript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript", "sourcegraph/scip-typescript"),
    ".tsx":  ("typescript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript", "sourcegraph/scip-typescript"),
    ".js":   ("javascript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript", "sourcegraph/scip-typescript"),
    ".jsx":  ("javascript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript", "sourcegraph/scip-typescript"),
    ".mjs":  ("javascript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript", "sourcegraph/scip-typescript"),
    ".cjs":  ("javascript", "scip-typescript", "npm install -g @sourcegraph/scip-typescript", "sourcegraph/scip-typescript"),
    ".go":   ("go",         "scip-go",         "go install github.com/sourcegraph/scip-go/...@latest", "sourcegraph/scip-go"),
    ".rs":   ("rust",       "scip-rust",       "cargo install scip-rust", "sourcegraph/scip-rust"),
    ".java": ("java",       "scip-java",       "see https://github.com/sourcegraph/scip-java", "sourcegraph/scip-java"),
    ".cpp":  ("cpp",        "scip-clang",      "brew install llvm", "sourcegraph/scip-clang"),
    ".hpp":  ("cpp",        "scip-clang",      "brew install llvm", "sourcegraph/scip-clang"),
    ".c":    ("c",          "scip-clang",      "brew install llvm", "sourcegraph/scip-clang"),
    ".h":    ("cpp",        "scip-clang",      "brew install llvm", "sourcegraph/scip-clang"),
}


def is_scip_available(lang: str) -> bool:
    """Check whether the SCIP indexer (binary or docker) for this language is available."""
    has_docker = shutil.which("docker") is not None
    for ext, (l, binary, _, docker_image) in EXTENSION_TO_SCIP.items():
        if l == lang:
            if shutil.which(binary) is not None:
                return True
            if has_docker and docker_image:
                return True
    return False


def detect_project_lang(path: Path, scip_languages: List[str]) -> Optional[str]:
    """
    Detect the primary language of a project folder by counting files.
    Only returns a language if it is in the user's SCIP_LANGUAGES list.
    """
    if not path.is_dir():
        ext = path.suffix
        lang = EXTENSION_TO_SCIP.get(ext, (None, None, None, None))[0]
        return lang if lang in scip_languages else None

    counts: Dict[str, int] = {}
    for ext, (lang, _, _, _) in EXTENSION_TO_SCIP.items():
        if lang not in scip_languages:
            continue
        counts[lang] = counts.get(lang, 0) + sum(
            1 for _ in path.rglob(f"*{ext}") if not file_path_has_ignore_dir_segment(Path(_), path)
        )

    if not counts:
        return None

    # Return the most frequent language
    return max(counts, key=counts.get)


def file_path_has_ignore_dir_segment(path: Path, root: Path) -> bool:
    """True if the path contains common ignored directory segments (node_modules, etc)."""
    try:
        rel = path.relative_to(root)
        parts = rel.parts
        ignore = {"node_modules", "vendor", ".git", "target", "build", "dist", "bin", "obj"}
        return any(p in ignore for p in parts)
    except ValueError:
        return False


class ScipIndexer:
    """
    Handles running the external SCIP indexer binaries.
    Takes a project path and language, runs the appropriate CLI, and
    returns the path to the resulting index.scip file.
    """

    def run(self, project_path: Path, lang: str, output_dir: Path) -> Optional[Path]:
        """
        Run the SCIP indexer for `lang` on `project_path`.
        Returns path to index.scip, or None if the indexer failed / is not installed.
        """
        binary, expected_binary, install_hint, docker_image = self._get_binary(lang)
        output_file = output_dir / "index.scip"
        
        if binary:
            cmd = self._build_command(lang, binary, project_path, output_file)
            if not cmd:
                warning_logger(f"No SCIP command template defined for language: {lang}")
                return None

            info_logger(f"Running local SCIP indexer: {' '.join(str(c) for c in cmd)}")
            try:
                result = subprocess.run(
                    cmd,
                    cwd=str(project_path),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0 and output_file.exists():
                    info_logger(f"SCIP index written to {output_file}")
                    return output_file
                warning_logger(f"Local SCIP indexer failed (code {result.returncode}). stderr: {result.stderr[:500]}")
            except Exception as e:
                warning_logger(f"Local SCIP indexer failed: {e}")

        # Fallback to Docker
        if docker_image and shutil.which("docker"):
            info_logger(f"Attempting SCIP indexing via Docker ({docker_image})...")
            try:
                # 1. Pre-run: for Go we often need 'go mod tidy'
                if lang == "go":
                    info_logger("Running 'go mod tidy' inside container first...")
                    subprocess.run(
                        ["docker", "run", "--rm", "-v", f"{project_path.resolve()}:/src", "-w", "/src", docker_image, "go", "mod", "tidy"],
                        capture_output=True, timeout=120
                    )

                # 2. Run indexer
                docker_cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{project_path.resolve()}:/src",
                    "-v", f"{output_dir.resolve()}:/out",
                    "-w", "/src",
                    docker_image,
                ]
                
                # Build the internal command (replacing output path with container-relative path)
                internal_cmd = self._build_command(lang, expected_binary, Path("/src"), Path("/out/index.scip"))
                if lang == "go" and not binary:
                    # Specific override for scip-go if binary not found locally
                    internal_cmd = ["scip-go", "index", ".", "--output", "/out/index.scip"]
                
                docker_cmd.extend(internal_cmd)
                
                info_logger(f"Running Docker command: {' '.join(docker_cmd)}")
                result = subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if result.returncode == 0:
                    if output_file.exists():
                        info_logger(f"SCIP index written to {output_file} via Docker")
                        return output_file
                    
                    # Workaround: Some SCIP indexers (like scip-rust) ignore --output and write to CWD
                    cwd_index = project_path / "index.scip"
                    if cwd_index.exists():
                        if cwd_index.resolve() != output_file.resolve():
                            shutil.move(str(cwd_index), str(output_file))
                        info_logger(f"SCIP index written to {output_file} via Docker (moved from project root)")
                        return output_file
                error_logger(f"Docker SCIP indexing failed (code {result.returncode}). stderr: {result.stderr[:500]}")
            except Exception as e:
                error_logger(f"Docker SCIP indexing failed: {e}")

        if not binary:
            warning_logger(f"SCIP indexer for '{lang}' not found locally or in Docker. Install with: {install_hint}")
        return None

    def _get_binary(self, lang: str) -> Tuple[Optional[str], str, str, Optional[str]]:
        for ext, (l, binary, install_hint, docker_image) in EXTENSION_TO_SCIP.items():
            if l == lang:
                found = shutil.which(binary)
                return found, binary, install_hint, docker_image
        return None, lang, "unknown language", None

    def _build_command(self, lang: str, binary: str, project_path: Path, output_file: Path) -> Optional[List]:
        """Build the CLI command for each supported SCIP indexer."""
        out = str(output_file)

        if lang == "python":
            return [binary, "index", ".", "--output", out]

        elif lang == "typescript":
            return [binary, "index", "--output", out]

        elif lang == "javascript":
            has_tsconfig = (project_path / "tsconfig.json").exists()
            if has_tsconfig:
                return [binary, "index", "--output", out]
            else:
                return [binary, "index", "--infer-tsconfig", "--output", out]

        elif lang == "go":
            return [binary, "index", ".", "--output", out]

        elif lang == "rust":
            return [binary, "index", "--output", out]

        elif lang == "java":
            return [binary, "index", "--output", out]

        elif lang in ("cpp", "c"):
            return [binary, f"--index-output-path={out}"]

        return None


class ScipIndexParser:
    """
    Parses an index.scip (Protobuf) file and extracts function calls,
    definitions, and relationships.
    """

    def parse(self, index_scip_path: Path, project_path: Path) -> Dict[str, Any]:
        try:
            from . import scip_pb2  # type: ignore
        except Exception as e:
            error_logger(f"Failed to import codegraphcontext.tools.scip_pb2: {e}")
            return {}

        try:
            with open(index_scip_path, "rb") as f:
                index = scip_pb2.Index()
                index.ParseFromString(f.read())
        except Exception as e:
            error_logger(f"Failed to parse SCIP index at {index_scip_path}: {e}")
            return {}

        symbol_def_table: Dict[str, Dict] = {}

        for doc in index.documents:
            for occ in doc.occurrences:
                if occ.symbol.startswith("local "):
                    continue
                role = getattr(occ, "symbol_roles", getattr(occ, "role", 0))
                if role & 1: # Definition
                    symbol_def_table[occ.symbol] = {
                        "file": doc.relative_path,
                        "line": occ.range[0] + 1 if occ.range else 0,
                    }
                    
                # Rust trait implementation extraction from symbol string
                # Pattern: ...impl#[StructName][TraitName]...
                import re
                impl_match = re.search(r"impl#\[([^\]]+)\]\[([^\]]+)\]", occ.symbol)
                if impl_match:
                    struct_name = impl_match.group(1)
                    trait_name = impl_match.group(2)
                    # We store this mapping globally to apply later
                    if "rust_impls" not in symbol_def_table:
                        symbol_def_table["rust_impls"] = {}
                    if struct_name not in symbol_def_table["rust_impls"]:
                        symbol_def_table["rust_impls"][struct_name] = set()
                    symbol_def_table["rust_impls"][struct_name].add(trait_name)

        for doc in index.documents:
            for sym_info in doc.symbols:
                if sym_info.symbol in symbol_def_table:
                    symbol_def_table[sym_info.symbol]["display_name"] = sym_info.display_name
                    symbol_def_table[sym_info.symbol]["documentation"] = "\n".join(sym_info.documentation)
                    symbol_def_table[sym_info.symbol]["kind"] = sym_info.kind
                    
                    bases = []
                    for rel in sym_info.relationships:
                        if rel.is_implementation:
                            bases.append(rel.symbol)
                    symbol_def_table[sym_info.symbol]["bases"] = bases

        for sym_info in index.external_symbols:
            if sym_info.symbol in symbol_def_table:
                symbol_def_table[sym_info.symbol]["display_name"] = sym_info.display_name
                symbol_def_table[sym_info.symbol]["documentation"] = "\n".join(sym_info.documentation)
                symbol_def_table[sym_info.symbol]["kind"] = sym_info.kind

        for sym, info in symbol_def_table.items():
            if sym == "rust_impls":
                continue
            if info.get("kind", 0) == 0:
                if sym.endswith("#"):
                    info["kind"] = 7
                elif sym.endswith("()."):
                    info["kind"] = 26 if "#" in sym else 17
            
            # Apply Rust implementations if available
            rust_impls = symbol_def_table.get("rust_impls", {})
            name = self._name_from_symbol(sym)
            if name in rust_impls and info.get("kind") in (7, 18, 49):
                info["bases"] = list(set(info.get("bases", []) + list(rust_impls[name])))

        files_data: Dict[str, Dict] = {}
        doc_source_lines: Dict[str, List[str]] = {}
        for doc in index.documents:
            src_path = project_path / doc.relative_path
            try:
                doc_source_lines[doc.relative_path] = src_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                doc_source_lines[doc.relative_path] = []

        for doc in index.documents:
            rel_path = doc.relative_path
            abs_path = str((project_path / rel_path).resolve())
            source_lines = doc_source_lines.get(rel_path, [])

            file_data: Dict[str, Any] = {
                "functions": [], "classes": [], "variables": [], "imports": [],
                "function_calls_scip": [], "module_level_calls_scip": [],
                "path": abs_path, "lang": self._lang_from_path(rel_path),
                "is_dependency": False,
            }

            definition_symbols_in_doc = []
            for occ in doc.occurrences:
                role = getattr(occ, "symbol_roles", getattr(occ, "role", 0))
                if role & 1:
                    definition_symbols_in_doc.append(occ)

            for occ in doc.occurrences:
                sym = occ.symbol
                if sym.startswith("local ") or sym == "rust_impls": continue
                line = occ.range[0] + 1 if occ.range else 0
                role = getattr(occ, "symbol_roles", getattr(occ, "role", 0))

                if role & 1:  # Definition
                    defn = symbol_def_table.get(sym, {})
                    kind = defn.get("kind", 0)
                    if kind == 0:
                        if sym.endswith("()."): kind = 17
                        elif "#" in sym:
                            if sym.endswith("#"): kind = 7
                            elif sym.endswith("()."): kind = 26
                    
                    name = self._name_from_symbol(sym)
                    args, return_type = self._parse_signature(defn.get("display_name", ""), kind)

                    node = {
                        "name": name, "line_number": line, "end_line": line,
                        "docstring": defn.get("documentation") or None,
                        "lang": file_data["lang"], "is_dependency": False,
                        "return_type": return_type, "args": args,
                    }

                    if kind in (26, 17):
                        node.update({"cyclomatic_complexity": 1, "decorators": [], "context": None, "class_context": None})
                        file_data["functions"].append(node)
                    elif kind in (7, 18, 49):
                        node["bases"] = [self._name_from_symbol(b) for b in defn.get("bases", [])]
                        node["context"] = None
                        file_data["classes"].append(node)
                    elif kind in (61, 15):
                        node.update({"value": None, "type": return_type, "context": None, "class_context": None})
                        file_data["variables"].append(node)

                else: # Reference
                    if sym not in symbol_def_table: continue
                    callee_info = symbol_def_table[sym]
                    r = list(occ.range)
                    ref_line_idx = r[0]
                    col_end = r[2] if len(r) > 2 else (r[1] if len(r) > 1 else 0)
                    src_line = source_lines[ref_line_idx] if ref_line_idx < len(source_lines) else ""
                    char_after = src_line[col_end:col_end + 1] if col_end < len(src_line) else ""

                    if char_after != "(": continue

                    caller_sym = self._find_enclosing_definition(line, definition_symbols_in_doc)
                    edge = {
                        "callee_symbol": sym, "callee_file": str((project_path / callee_info["file"]).resolve()),
                        "callee_line": callee_info["line"], "callee_name": self._name_from_symbol(sym),
                        "callee_kind": callee_info.get("kind", 0), "ref_line": line, "caller_file": abs_path,
                    }

                    if caller_sym:
                        caller_info = symbol_def_table.get(caller_sym, {})
                        edge.update({"caller_symbol": caller_sym, "caller_line": caller_info.get("line", 0)})
                        file_data["function_calls_scip"].append(edge)
                    else:
                        edge.update({"caller_symbol": None, "caller_line": 0})
                        file_data["module_level_calls_scip"].append(edge)

            files_data[abs_path] = file_data

        return {"files": files_data, "symbol_table": symbol_def_table}

    def _name_from_symbol(self, symbol: str) -> str:
        import re
        s = symbol.rstrip(".#")
        s = re.sub(r"\(\)\.?$", "", s)
        parts = re.split(r'[/#]', s)
        return parts[-1] if parts else symbol

    def _lang_from_path(self, rel_path: str) -> str:
        ext_map = {".py": "python", ".ipynb": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript", ".go": "go", ".rs": "rust"}
        return ext_map.get(Path(rel_path).suffix, "unknown")

    def _parse_signature(self, display_name: str, kind: int) -> Tuple[List[str], Optional[str]]:
        import re
        args: List[str] = []
        return_type: Optional[str] = None
        if not display_name: return args, return_type
        if "->" in display_name:
            parts = display_name.rsplit("->", 1)
            return_type = parts[1].strip().rstrip(":")
        param_match = re.search(r"\(([^)]*)\)", display_name)
        if param_match:
            for param in param_match.group(1).split(","):
                name = param.strip().split(":")[0].split("=")[0].strip().lstrip("*")
                if name: args.append(name)
        return args, return_type

    def _find_enclosing_definition(self, ref_line: int, definition_occurrences: list) -> Optional[str]:
        best_enclosing: Optional[str] = None
        best_enclosing_start = -1
        for occ in definition_occurrences:
            if occ.symbol.endswith("/"): continue
            er = list(getattr(occ, "enclosing_range", []))
            if not er: continue
            enc_start, enc_end = (er[0] + 1, er[2] + 1) if len(er) == 4 else (er[0] + 1, er[0] + 1)
            if enc_start <= ref_line <= enc_end and enc_start > best_enclosing_start:
                best_enclosing = occ.symbol
                best_enclosing_start = enc_start
        return best_enclosing
