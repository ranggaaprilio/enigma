"""Epic 13: detect whether a repository's dev server already proxies API requests, so Enigma
can tell a user "your frontend calls `/api/...`, but nothing forwards that to your backend"
*before* they hit a failed request in the browser.

Pure and read-only, mirroring `run_detection.candidates_from_manifests`'s shape: framework
identity comes only from *which config filename is present*, never from parsing its contents as
JS (evaluating it would violate the same "never execute repository code" rule that keeps mise
tasks read via `tomllib` instead of `mise task ls`). Presence of the *key* that would carry a
proxy (`server.proxy`, `devServer.proxy`, `rewrites`, ...) is treated as "configured" even when
its value is built by a function this module cannot safely evaluate -- under-reporting a false
"not configured" would be actively wrong advice; a computed proxy that turns out to point
somewhere unhelpful is the user's problem to notice, not this module's to guess at.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Config files worth reading, per framework. Order matters only for readability; detection
# reports on whichever ones are actually present.
VITE_CONFIG_FILES = (
    "vite.config.js",
    "vite.config.ts",
    "vite.config.mjs",
    "vite.config.mts",
    "vite.config.cjs",
    "vite.config.cts",
)
NEXT_CONFIG_FILES = ("next.config.js", "next.config.ts", "next.config.mjs", "next.config.cjs")
WEBPACK_CONFIG_FILES = (
    "webpack.config.js",
    "webpack.config.ts",
    "webpack.config.cjs",
    "webpack.config.mjs",
    "webpack.dev.config.js",
)
VUE_CONFIG_FILES = ("vue.config.js",)
NUXT_CONFIG_FILES = ("nuxt.config.js", "nuxt.config.ts")
ANGULAR_CONFIG_FILES = ("angular.json",)
CRA_CONFIG_FILES = ("package.json",)

# The fixed-name files this module ever reads in one bundle. `angular.json` can name a *second*,
# arbitrarily-named file (its `proxyConfig`) -- that one is not in this tuple because its name is
# only known after `angular.json` itself has been read; see `angular_proxy_config_path`.
PROXY_CONFIG_FILES: tuple[str, ...] = tuple(
    dict.fromkeys(
        VITE_CONFIG_FILES
        + NEXT_CONFIG_FILES
        + WEBPACK_CONFIG_FILES
        + VUE_CONFIG_FILES
        + NUXT_CONFIG_FILES
        + ANGULAR_CONFIG_FILES
        + CRA_CONFIG_FILES
    )
)

# A config far larger than this is not a hand-written dev-server config worth regexing.
MAX_CONFIG_BYTES = 20_000


@dataclass(frozen=True)
class ProxyConfig:
    """One dev-proxy configuration finding for one file."""

    framework: str  # "vite" | "next" | "webpack" | "vue" | "nuxt" | "angular" | "cra"
    source_file: str
    configured: bool
    targets: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    reason: str = ""


def _strip_comments(text: str) -> str:
    """Conservative removal of `//` line comments and `/* */` block comments, so a commented-out
    proxy example doesn't read as "configured". Deliberately does not try to be a real JS
    tokenizer (it would have to evaluate to do that correctly) -- it just avoids the single most
    common false positive: a copy-pasted, commented-out `proxy: {...}` block from documentation.
    """
    # Block comments first (non-greedy), then line comments. This can misfire inside a string
    # literal that itself contains `//` or `/*` -- an acceptable, rare false negative (reports
    # "configured" when it might not be) rather than a false positive in the other direction.
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # `(?<!:)` protects a URL's own "//" (in "http://", "https://") from being misread as the
    # start of a line comment -- an ordinary "// comment" is never preceded by a colon.
    return re.sub(r"(?<!:)//[^\n]*", "", without_blocks)


def _truncate(text: str) -> str:
    return text[:MAX_CONFIG_BYTES]


_TARGET_PATTERN = re.compile(r"target\s*:\s*['\"]([^'\"]+)['\"]")
_STRING_SHORTHAND_PATTERN = re.compile(r"proxy\s*:\s*['\"]([^'\"]+)['\"]")
_PATH_KEY_PATTERN = re.compile(r"""['"](/[A-Za-z0-9_\-./*]*)['"]\s*:""")


def _extract_targets_and_paths(
    text: str, *, window_start: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    window = text[window_start : window_start + 4000]
    targets = tuple(dict.fromkeys(_TARGET_PATTERN.findall(window)))
    if not targets:
        targets = tuple(dict.fromkeys(_STRING_SHORTHAND_PATTERN.findall(window)))
    paths = tuple(dict.fromkeys(_PATH_KEY_PATTERN.findall(window)))
    return targets, paths


def _vite_config(source_file: str, content: str) -> ProxyConfig:
    text = _truncate(_strip_comments(content))
    has_server = re.search(r"\bserver\s*:\s*\{", text) is not None
    # Any value form -- an object literal, an array, a function call, or a bare identifier --
    # counts. Requiring an object literal specifically would under-report a proxy built by a
    # helper function (`proxy: makeProxyConfig()`), which is still a real, working proxy.
    proxy_match = re.search(r"\bproxy\s*:", text)
    configured = has_server and proxy_match is not None
    targets, paths = ((), ())
    if configured:
        targets, paths = _extract_targets_and_paths(text, window_start=proxy_match.start())
    return ProxyConfig(
        framework="vite",
        source_file=source_file,
        configured=configured,
        targets=targets,
        paths=paths,
        reason="server.proxy" if configured else "no server.proxy block found",
    )


def _next_config(source_file: str, content: str) -> ProxyConfig:
    text = _truncate(_strip_comments(content))
    match = re.search(r"\brewrites\s*(?::|\()", text)
    configured = match is not None
    targets, paths = ((), ())
    if configured:
        targets, paths = _extract_targets_and_paths(text, window_start=match.start())
    return ProxyConfig(
        framework="next",
        source_file=source_file,
        configured=configured,
        targets=targets,
        paths=paths,
        reason="rewrites()" if configured else "no rewrites() found",
    )


def _webpack_or_vue_config(framework: str, source_file: str, content: str) -> ProxyConfig:
    text = _truncate(_strip_comments(content))
    has_dev_server = re.search(r"\bdevServer\s*:\s*\{", text) is not None
    # Any value form -- webpack 5 also accepts an array of proxy rules (`proxy: [...]`), and a
    # computed value (`proxy: makeProxyConfig()`) is still a real, working proxy.
    proxy_match = re.search(r"\bproxy\s*:", text)
    configured = has_dev_server and proxy_match is not None
    targets, paths = ((), ())
    if configured:
        targets, paths = _extract_targets_and_paths(text, window_start=proxy_match.start())
    return ProxyConfig(
        framework=framework,
        source_file=source_file,
        configured=configured,
        targets=targets,
        paths=paths,
        reason="devServer.proxy" if configured else "no devServer.proxy found",
    )


def _nuxt_config(source_file: str, content: str) -> ProxyConfig:
    text = _truncate(_strip_comments(content))
    configured = bool(
        re.search(r"\bdevProxy\b", text)
        or re.search(r"routeRules[\s\S]{0,2000}?proxy\s*:\s*['\"]", text)
        or "@nuxtjs/proxy" in text
    )
    targets, paths = ((), ())
    if configured:
        match = re.search(r"\bdevProxy\b", text) or re.search(r"routeRules", text)
        if match:
            targets, paths = _extract_targets_and_paths(text, window_start=match.start())
    return ProxyConfig(
        framework="nuxt",
        source_file=source_file,
        configured=configured,
        targets=targets,
        paths=paths,
        reason="devProxy/routeRules proxy" if configured else "no proxy configuration found",
    )


def angular_proxy_config_path(angular_json_content: str) -> str | None:
    """The file `angular.json` names via `architect.serve.options.proxyConfig`, for a caller to
    read in a second, targeted round trip -- or `None` if `angular.json` doesn't parse or
    declares none. Never raises on malformed JSON; that is simply "no proxy configured"."""
    try:
        data = json.loads(angular_json_content)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for project in (data.get("projects") or {}).values():
        if not isinstance(project, dict):
            continue
        architect = project.get("architect") or project.get("targets") or {}
        serve = architect.get("serve") if isinstance(architect, dict) else None
        options = serve.get("options") if isinstance(serve, dict) else None
        path = options.get("proxyConfig") if isinstance(options, dict) else None
        if isinstance(path, str) and path:
            return path
    return None


def _angular_config(
    source_file: str, angular_json_content: str, proxy_file_content: str | None
) -> ProxyConfig:
    proxy_path = angular_proxy_config_path(angular_json_content)
    if proxy_path is None:
        return ProxyConfig(
            framework="angular",
            source_file=source_file,
            configured=False,
            reason="no architect.serve.options.proxyConfig declared",
        )
    if proxy_file_content is None:
        # angular.json names a proxy file, but it wasn't (yet) read -- report unconfigured
        # rather than guessing; the caller is expected to fetch it in a second pass.
        return ProxyConfig(
            framework="angular",
            source_file=source_file,
            configured=False,
            reason=f"proxyConfig names {proxy_path!r} but it could not be read",
        )
    try:
        proxy_data = json.loads(proxy_file_content)
    except (json.JSONDecodeError, ValueError):
        # Falls back to a regex rather than raising -- angular's proxy.conf.json sometimes has
        # trailing commas or comments and isn't strictly valid JSON.
        configured = bool(re.search(r'"?/[^"\s]*"?\s*:\s*\{', proxy_file_content))
        return ProxyConfig(
            framework="angular",
            source_file=proxy_path,
            configured=configured,
            reason="proxyConfig file present (non-JSON, regex fallback)",
        )
    targets = tuple(
        dict.fromkeys(
            entry.get("target")
            for entry in proxy_data.values()
            if isinstance(entry, dict) and isinstance(entry.get("target"), str)
        )
        if isinstance(proxy_data, dict)
        else ()
    )
    paths = tuple(proxy_data.keys()) if isinstance(proxy_data, dict) else ()
    return ProxyConfig(
        framework="angular",
        source_file=proxy_path,
        configured=bool(paths),
        targets=targets,
        paths=paths,
        reason="proxyConfig" if paths else "proxyConfig file is empty",
    )


def _cra_config(source_file: str, package_json_content: str) -> ProxyConfig:
    try:
        data = json.loads(package_json_content)
    except (json.JSONDecodeError, ValueError):
        return ProxyConfig(
            framework="cra",
            source_file=source_file,
            configured=False,
            reason="unparsable package.json",
        )
    proxy = data.get("proxy") if isinstance(data, dict) else None
    if isinstance(proxy, str) and proxy:
        return ProxyConfig(
            framework="cra", source_file=source_file, configured=True, targets=(proxy,),
            reason='"proxy" field',
        )
    return ProxyConfig(
        framework="cra", source_file=source_file, configured=False, reason='no "proxy" field'
    )


def proxy_configs_from_manifests(
    files: dict[str, str], *, angular_proxy_files: dict[str, str] | None = None
) -> list[ProxyConfig]:
    """`files` maps a path (relative to the service's own working directory) to its content --
    normally the output of one bundle read over `PROXY_CONFIG_FILES`. `angular_proxy_files` maps
    the path angular.json names (via `architect.serve.options.proxyConfig`) to its content, from
    a second, targeted read (`RunDetectionService.read_proxy_configs` performs that read; this
    function stays pure and never does IO itself).
    """
    angular_proxy_files = angular_proxy_files or {}
    found: list[ProxyConfig] = []

    for name in VITE_CONFIG_FILES:
        if name in files:
            found.append(_vite_config(name, files[name]))
    for name in NEXT_CONFIG_FILES:
        if name in files:
            found.append(_next_config(name, files[name]))
    for name in WEBPACK_CONFIG_FILES:
        if name in files:
            found.append(_webpack_or_vue_config("webpack", name, files[name]))
    for name in VUE_CONFIG_FILES:
        if name in files:
            found.append(_webpack_or_vue_config("vue", name, files[name]))
    for name in NUXT_CONFIG_FILES:
        if name in files:
            found.append(_nuxt_config(name, files[name]))
    for name in ANGULAR_CONFIG_FILES:
        if name in files:
            proxy_path = angular_proxy_config_path(files[name])
            proxy_content = angular_proxy_files.get(proxy_path) if proxy_path else None
            found.append(_angular_config(name, files[name], proxy_content))
    if "package.json" in files:
        cra = _cra_config("package.json", files["package.json"])
        if cra.configured:
            found.append(cra)

    return found


# ---------------------------------------------------------------------------
# Snippets -- module constants, same rationale as `run_presets.py`: nothing about these is
# per-workspace, so they need no database row.
# ---------------------------------------------------------------------------

_SNIPPETS: dict[str, str] = {
    "vite": (
        "export default defineConfig({{\n"
        "  server: {{\n"
        "    proxy: {{\n"
        "      '{path}': 'http://localhost:{api_port}',\n"
        "    }},\n"
        "  }},\n"
        "}})\n"
    ),
    "next": (
        "async rewrites() {{\n"
        "  return [\n"
        "    {{ source: '{path}/:path*', "
        "destination: 'http://localhost:{api_port}{path}/:path*' }},\n"
        "  ]\n"
        "}}\n"
    ),
    "webpack": (
        "devServer: {{\n"
        "  proxy: {{\n"
        "    '{path}': 'http://localhost:{api_port}',\n"
        "  }},\n"
        "}}\n"
    ),
    "vue": (
        "module.exports = {{\n"
        "  devServer: {{\n"
        "    proxy: {{\n"
        "      '{path}': {{ target: 'http://localhost:{api_port}' }},\n"
        "    }},\n"
        "  }},\n"
        "}}\n"
    ),
    "nuxt": (
        "routeRules: {{\n"
        "  '{path}/**': {{ proxy: 'http://localhost:{api_port}/**' }},\n"
        "}}\n"
    ),
    "angular": (
        "{{\n"
        '  "{path}": {{\n'
        '    "target": "http://localhost:{api_port}",\n'
        '    "secure": false\n'
        "  }}\n"
        "}}\n"
    ),
    "cra": '"proxy": "http://localhost:{api_port}"\n',
}


def snippet_for(framework: str, *, api_port: int, path_prefix: str = "/api") -> str:
    template = _SNIPPETS.get(framework)
    if template is None:
        return ""
    return template.format(api_port=api_port, path=path_prefix)
