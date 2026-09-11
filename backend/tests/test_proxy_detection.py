"""Epic 13: dev-proxy configuration detection -- pure, no sandbox, never evaluates JS."""


from app.services.proxy_detection import (
    PROXY_CONFIG_FILES,
    angular_proxy_config_path,
    proxy_configs_from_manifests,
    snippet_for,
)

# ---------------------------------------------------------------------------
# Vite
# ---------------------------------------------------------------------------


def test_vite_configured_detects_server_proxy():
    content = (
        "export default defineConfig({\n"
        "  server: {\n"
        "    proxy: {\n"
        "      '/api': 'http://localhost:8000',\n"
        "    },\n"
        "  },\n"
        "})\n"
    )
    configs = proxy_configs_from_manifests({"vite.config.ts": content})
    assert len(configs) == 1
    assert configs[0].framework == "vite"
    assert configs[0].configured is True
    assert "/api" in configs[0].paths


def test_vite_absent_when_no_proxy_key():
    content = "export default defineConfig({ server: { port: 5173 } })\n"
    configs = proxy_configs_from_manifests({"vite.config.ts": content})
    assert configs[0].configured is False


def test_vite_commented_out_proxy_counts_as_absent():
    content = (
        "export default defineConfig({\n"
        "  server: {\n"
        "    // proxy: { '/api': 'http://localhost:8000' },\n"
        "    port: 5173,\n"
        "  },\n"
        "})\n"
    )
    configs = proxy_configs_from_manifests({"vite.config.ts": content})
    assert configs[0].configured is False


def test_vite_block_commented_out_proxy_counts_as_absent():
    content = (
        "export default defineConfig({\n"
        "  server: {\n"
        "    /* proxy: { '/api': 'http://localhost:8000' }, */\n"
        "    port: 5173,\n"
        "  },\n"
        "})\n"
    )
    configs = proxy_configs_from_manifests({"vite.config.ts": content})
    assert configs[0].configured is False


def test_vite_extracts_the_target_url():
    content = (
        "export default defineConfig({ server: { proxy: { '/api': "
        "{ target: 'http://localhost:9000' } } } })\n"
    )
    configs = proxy_configs_from_manifests({"vite.config.ts": content})
    assert configs[0].targets == ("http://localhost:9000",)


# ---------------------------------------------------------------------------
# Next.js
# ---------------------------------------------------------------------------


def test_next_rewrites_as_async_method():
    content = (
        "module.exports = {\n"
        "  async rewrites() {\n"
        "    return [{ source: '/api/:path*', destination: 'http://localhost:8000/api/:path*' }]\n"
        "  },\n"
        "}\n"
    )
    configs = proxy_configs_from_manifests({"next.config.js": content})
    assert configs[0].configured is True


def test_next_rewrites_as_plain_key():
    content = "export default { rewrites: async () => [] }\n"
    configs = proxy_configs_from_manifests({"next.config.js": content})
    assert configs[0].configured is True


def test_next_absent_with_no_rewrites():
    content = "module.exports = { reactStrictMode: true }\n"
    configs = proxy_configs_from_manifests({"next.config.js": content})
    assert configs[0].configured is False


# ---------------------------------------------------------------------------
# Webpack / Vue
# ---------------------------------------------------------------------------


def test_webpack_object_form():
    content = "module.exports = { devServer: { proxy: { '/api': 'http://localhost:8000' } } }\n"
    configs = proxy_configs_from_manifests({"webpack.config.js": content})
    assert configs[0].configured is True
    assert configs[0].framework == "webpack"


def test_webpack_5_array_form():
    content = (
        "module.exports = { devServer: { proxy: [{ context: ['/api'], "
        "target: 'http://localhost:8000' }] } }\n"
    )
    configs = proxy_configs_from_manifests({"webpack.config.js": content})
    assert configs[0].configured is True


def test_webpack_absent_without_dev_server():
    content = "module.exports = { mode: 'development' }\n"
    configs = proxy_configs_from_manifests({"webpack.config.js": content})
    assert configs[0].configured is False


def test_vue_config():
    content = (
        "module.exports = { devServer: { proxy: { '/api': "
        "{ target: 'http://localhost:8000' } } } }\n"
    )
    configs = proxy_configs_from_manifests({"vue.config.js": content})
    assert configs[0].framework == "vue"
    assert configs[0].configured is True


# ---------------------------------------------------------------------------
# Nuxt
# ---------------------------------------------------------------------------


def test_nuxt_dev_proxy():
    content = "export default { devProxy: { '/api': 'http://localhost:8000' } }\n"
    configs = proxy_configs_from_manifests({"nuxt.config.ts": content})
    assert configs[0].configured is True


def test_nuxt_route_rules():
    content = (
        "export default { routeRules: { '/api/**': "
        "{ proxy: 'http://localhost:8000/**' } } }\n"
    )
    configs = proxy_configs_from_manifests({"nuxt.config.ts": content})
    assert configs[0].configured is True


def test_nuxt_proxy_module():
    content = "export default { modules: ['@nuxtjs/proxy'] }\n"
    configs = proxy_configs_from_manifests({"nuxt.config.ts": content})
    assert configs[0].configured is True


def test_nuxt_absent():
    content = "export default { modules: ['@nuxtjs/tailwindcss'] }\n"
    configs = proxy_configs_from_manifests({"nuxt.config.ts": content})
    assert configs[0].configured is False


# ---------------------------------------------------------------------------
# Angular
# ---------------------------------------------------------------------------


def test_angular_proxy_config_path_extracted():
    angular_json = """
    {
      "projects": {
        "app": {
          "architect": {
            "serve": {
              "options": {
                "proxyConfig": "proxy.conf.json"
              }
            }
          }
        }
      }
    }
    """
    assert angular_proxy_config_path(angular_json) == "proxy.conf.json"


def test_angular_no_proxy_config_declared():
    angular_json = '{"projects": {"app": {"architect": {"serve": {"options": {}}}}}}'
    assert angular_proxy_config_path(angular_json) is None


def test_angular_unparsable_json_returns_none_not_raise():
    assert angular_proxy_config_path("{not valid json") is None


def test_angular_full_detection_with_proxy_file():
    angular_json = """
    {"projects": {"app": {"architect": {"serve": {"options": {"proxyConfig": "proxy.conf.json"}}}}}}
    """
    proxy_conf = '{"/api": {"target": "http://localhost:8000", "secure": false}}'
    configs = proxy_configs_from_manifests(
        {"angular.json": angular_json},
        angular_proxy_files={"proxy.conf.json": proxy_conf},
    )
    assert configs[0].configured is True
    assert configs[0].targets == ("http://localhost:8000",)
    assert "/api" in configs[0].paths


def test_angular_names_a_file_but_it_was_not_read():
    angular_json = """
    {"projects": {"app": {"architect": {"serve": {"options": {"proxyConfig": "proxy.conf.json"}}}}}}
    """
    configs = proxy_configs_from_manifests({"angular.json": angular_json})
    assert configs[0].configured is False


# ---------------------------------------------------------------------------
# Create React App
# ---------------------------------------------------------------------------


def test_cra_proxy_field():
    package_json = '{"name": "app", "proxy": "http://localhost:8000"}'
    configs = proxy_configs_from_manifests({"package.json": package_json})
    assert len(configs) == 1
    assert configs[0].framework == "cra"
    assert configs[0].configured is True


def test_cra_no_proxy_field_produces_no_entry():
    package_json = '{"name": "app"}'
    configs = proxy_configs_from_manifests({"package.json": package_json})
    assert configs == []  # unconfigured CRA isn't reported -- package.json exists for every repo


# ---------------------------------------------------------------------------
# Never evaluated: a computed proxy is reported configured but with no extractable target
# ---------------------------------------------------------------------------


def test_computed_proxy_reports_configured_with_no_targets():
    content = "export default defineConfig({ server: { proxy: makeProxyConfig() } })\n"
    configs = proxy_configs_from_manifests({"vite.config.ts": content})
    assert configs[0].configured is True
    assert configs[0].targets == ()


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_a_huge_minified_config_is_truncated_and_still_fast():
    content = "export default defineConfig({" + ("x" * 500_000) + "server:{proxy:{'/api':'http://localhost:8000'}}})"
    import time

    start = time.time()
    configs = proxy_configs_from_manifests({"vite.config.ts": content})
    elapsed = time.time() - start
    assert elapsed < 1.0
    # The proxy block sits past the truncation window in this adversarial case, so it correctly
    # reports "not configured" rather than scanning the whole 500KB string.
    assert configs[0].configured is False


def test_snippet_for_every_known_framework_contains_the_api_port():
    for framework in ("vite", "next", "webpack", "vue", "nuxt", "angular", "cra"):
        snippet = snippet_for(framework, api_port=9000)
        assert snippet
        assert "9000" in snippet


def test_snippet_for_unknown_framework_is_empty():
    assert snippet_for("svelte", api_port=8000) == ""


def test_proxy_config_files_tuple_has_no_duplicates():
    assert len(PROXY_CONFIG_FILES) == len(set(PROXY_CONFIG_FILES))
