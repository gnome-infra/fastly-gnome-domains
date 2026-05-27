# fastly-gnome-domains

Fastly CDN configuration for GNOME custom domains — GitLab Pages reverse proxying and domain redirects managed as a single Fastly service via Terraform.

## Adding a New Domain

### Simple GitLab Pages proxy

Create a TOML file in `domains/proxy/`:

```toml
[[pages]]
domain = "example.gnome.org"
origin = "https://teams.pages.gitlab.gnome.org/Websites/example.gnome.org/"
```

No VCL changes needed.

### Simple redirect

Add to an existing file or create a new one in `domains/redirects/`:

```toml
[[redirects]]
domain = "old.gnome.org"
target = "https://new.gnome.org"
type = "permanent"
preserve_path = true
```

No VCL changes needed.

### Domain with custom routing

For path redirects, subpath proxies, regex rewrites, or synthetic responses,
add `vcl_snippet` and the relevant fields:

```toml
[[pages]]
domain = "example.gnome.org"
origin = "https://teams.pages.gitlab.gnome.org/Websites/example.gnome.org/"
vcl_snippet = "example"
custom_vcl = """
  if (req.url ~ "^/old/([^/]+)/$") {
    error 751 "https://example.gnome.org/" re.group.1 "/";
  }
"""

[pages.path_redirects]
"/legacy" = "https://example.gnome.org/new"

[pages.subpath_proxies]
"/docs/" = "/Websites/other-project/"
```

Fields are emitted in this order: `subpath_proxies` → `custom_vcl` → `path_redirects` → `default_redirect`.

For synthetic responses (JSON endpoints, 302 redirects), add `custom_vcl_error`:

```toml
custom_vcl_error = """
  if (obj.status == 760) {
    set obj.status = 200;
    set obj.http.Content-Type = "application/json";
    synthetic {"{ "key": "value" }"};
    return(deliver);
  }
"""
```

**VCL long strings:** `{"` and `"}` are VCL delimiters. Never put `"` immediately
before `}` in JSON — add a space: `{ "key": "value" }`.

Both `main.vcl` and the per-domain VCL files are generated automatically —
no manual VCL or Terraform changes needed.

Subroutine names: `vcl_snippet` value with hyphens replaced by underscores + `_recv` / `_error`.
