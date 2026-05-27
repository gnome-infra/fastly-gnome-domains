#!/usr/bin/env python3
"""
Read domains/proxy/*.toml and domains/redirects/*.toml, then generate:
  1. terraform.tfvars.json — domain mappings for Terraform edge dictionaries
  2. VCL files for domains with vcl_snippet (from path_redirects,
     subpath_proxies, custom_vcl, or any combination)

Usage:
    python generate-tfvars.py                  # writes terraform.tfvars.json + VCL
    python generate-tfvars.py --check          # validates TOML without writing
    python generate-tfvars.py --output out.json # write tfvars to a custom path
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

SCRIPT_DIR = Path(__file__).parent
PROXY_DIR = SCRIPT_DIR / "domains" / "proxy"
REDIRECT_DIR = SCRIPT_DIR / "domains" / "redirects"
VCL_DIR = SCRIPT_DIR / "vcl"


# ── Data structures ──

class VclConfig:
    """A domain entry that needs a generated VCL file."""

    def __init__(
        self,
        domain: str,
        aliases: list[str],
        vcl_snippet: str,
        path_redirects: dict[str, str],
        subpath_proxies: dict[str, str],
        custom_vcl: str,
        custom_vcl_error: str,
        default_redirect: str,
        source_file: str,
        source_dir: str,
        origin_host: str = "",
    ):
        self.domain = domain
        self.aliases = aliases
        self.vcl_snippet = vcl_snippet
        self.path_redirects = path_redirects
        self.subpath_proxies = subpath_proxies
        self.custom_vcl = custom_vcl
        self.custom_vcl_error = custom_vcl_error
        self.default_redirect = default_redirect
        self.source_file = source_file
        self.source_dir = source_dir
        self.origin_host = origin_host

    def has_vcl_content(self) -> bool:
        return bool(
            self.path_redirects
            or self.subpath_proxies
            or self.custom_vcl
            or self.custom_vcl_error
            or self.default_redirect
        )


# ── Parsing ──

def parse_proxy_configs() -> tuple[dict[str, str], list[str], list[VclConfig]]:
    """Parse proxy TOML files. Returns (domain_to_path, domain_list, vcl_configs)."""
    domain_to_path: dict[str, str] = {}
    domain_to_origin_host: dict[str, str] = {}
    all_domains: list[str] = []
    vcl_configs: list[VclConfig] = []

    for toml_file in sorted(PROXY_DIR.glob("*.toml")):
        with open(toml_file, "rb") as fp:
            config = tomllib.load(fp)

        for page in config.get("pages", []):
            domain = page["domain"]
            origin = page.get("origin", "")
            aliases = page.get("aliases", [])
            origin_host = ""

            all_domains.append(domain)
            for alias in aliases:
                all_domains.append(alias)

            if origin:
                url = urlparse(origin)
                pages_path = url.path.rstrip("/") + "/"
                origin_host = url.hostname or ""
                domain_to_path[domain] = pages_path
                domain_to_origin_host[domain] = origin_host
                for alias in aliases:
                    domain_to_path[alias] = pages_path
                    domain_to_origin_host[alias] = origin_host

            vcl_snippet = page.get("vcl_snippet", "")
            if vcl_snippet:
                vc = VclConfig(
                    domain=domain,
                    aliases=aliases,
                    vcl_snippet=vcl_snippet,
                    path_redirects=page.get("path_redirects", {}),
                    subpath_proxies=page.get("subpath_proxies", {}),
                    custom_vcl=page.get("custom_vcl", ""),
                    custom_vcl_error=page.get("custom_vcl_error", ""),
                    default_redirect=page.get("default_redirect", ""),
                    source_file=toml_file.name,
                    source_dir="proxy",
                    origin_host=origin_host,
                )
                vcl_configs.append(vc)

    return domain_to_path, domain_to_origin_host, all_domains, vcl_configs


def parse_redirect_configs() -> tuple[dict[str, str], dict[str, str], list[str], list[VclConfig]]:
    """Parse redirect TOML files. Returns (simple_redirects, preserve_path, domain_list, vcl_configs)."""
    simple_redirects: dict[str, str] = {}
    preserve_path: dict[str, str] = {}
    all_domains: list[str] = []
    vcl_configs: list[VclConfig] = []

    for toml_file in sorted(REDIRECT_DIR.glob("*.toml")):
        with open(toml_file, "rb") as fp:
            config = tomllib.load(fp)

        for redirect in config.get("redirects", []):
            rtype = redirect.get("type", "permanent")

            domains = list(redirect.get("domains", []))
            if "domain" in redirect:
                domains.append(redirect["domain"])

            target = redirect.get("target", "")
            should_preserve = redirect.get("preserve_path", False)

            for domain in domains:
                all_domains.append(domain)

                if target and rtype in ("permanent", "temporary"):
                    simple_redirects[domain] = target
                    if should_preserve:
                        preserve_path[domain] = "true"

            vcl_snippet = redirect.get("vcl_snippet", "")
            if vcl_snippet:
                primary_domain = redirect.get("domain", domains[0] if domains else "")
                vc = VclConfig(
                    domain=primary_domain,
                    aliases=[],
                    vcl_snippet=vcl_snippet,
                    path_redirects=redirect.get("path_redirects", {}),
                    subpath_proxies={},
                    custom_vcl=redirect.get("custom_vcl", ""),
                    custom_vcl_error=redirect.get("custom_vcl_error", ""),
                    default_redirect=redirect.get("default_redirect", ""),
                    source_file=toml_file.name,
                    source_dir="redirects",
                )
                vcl_configs.append(vc)

    return simple_redirects, preserve_path, all_domains, vcl_configs


# ── VCL Generation ──

def _escape_vcl_regex(path: str) -> str:
    """Escape special regex characters in a path for use in VCL regex."""
    return re.sub(r'([+.*?{}()|^$\[\]\\])', r'\\\1', path)


def _vcl_sub_name(snippet_name: str, suffix: str = "recv") -> str:
    """Convert a vcl_snippet name to a valid VCL subroutine name."""
    return snippet_name.replace("-", "_") + "_" + suffix


def generate_vcl(vc: VclConfig) -> str | None:
    """Generate VCL file content from a VclConfig.

    Produces a _recv subroutine (always) and optionally a _error subroutine
    (if custom_vcl_error is present). Returns None if there's nothing to generate.
    """
    if not vc.has_vcl_content():
        return None

    recv_name = _vcl_sub_name(vc.vcl_snippet, "recv")

    host_domains = [vc.domain] + vc.aliases
    if len(host_domains) == 1:
        host_check = f'req.http.Host != "{vc.domain}"'
    else:
        conditions = " && ".join(f'req.http.Host != "{d}"' for d in host_domains)
        host_check = conditions

    lines: list[str] = []
    lines.append(f"# Generated from {vc.source_file} — do not edit manually")
    lines.append(f"#")
    lines.append(f"# To modify these rules, edit domains/{vc.source_dir}/{vc.source_file}")
    lines.append(f"# and run: python generate-tfvars.py")
    lines.append(f"")
    lines.append(f"sub {recv_name} {{")
    lines.append(f"  if ({host_check}) {{")
    lines.append(f"    return;")
    lines.append(f"  }}")

    # 1. Subpath proxies (highest priority — route to different backends)
    if vc.subpath_proxies:
        lines.append(f"")
        for subpath, target_path in vc.subpath_proxies.items():
            escaped = _escape_vcl_regex(subpath)
            lines.append(f'  if (req.url ~ "^{escaped}") {{')
            lines.append(f"    set req.backend = F_gitlab_pages;")
            lines.append(f'    set req.http.X-Orig-Host = req.http.Host;')
            lines.append(f'    set req.http.Host = "{vc.origin_host}";')
            lines.append(f'    set req.url = "{target_path}" regsub(req.url, "^{escaped}", "");')
            lines.append(f"    unset req.http.X-Forwarded-Host;")
            lines.append(f"    return(pass);")
            lines.append(f"  }}")

    # 2. Custom VCL (raw VCL for regex rewrites, synthetic triggers, etc.)
    if vc.custom_vcl:
        lines.append(f"")
        for custom_line in vc.custom_vcl.strip("\n").splitlines():
            lines.append(custom_line.rstrip())

    # 3. Path redirects (simple prefix → target)
    if vc.path_redirects:
        lines.append(f"")
        for path, target in vc.path_redirects.items():
            escaped = _escape_vcl_regex(path)
            if path == "/":
                lines.append(f'  if (req.url ~ "^/$") {{')
            elif path.endswith("/"):
                lines.append(f'  if (req.url ~ "^{escaped}") {{')
            else:
                lines.append(f'  if (req.url ~ "^{escaped}(/|$)") {{')
            lines.append(f'    error 751 "{target}";')
            lines.append(f"  }}")

    # 4. Default redirect (catch-all)
    if vc.default_redirect:
        lines.append(f"")
        lines.append(f'  error 751 "{vc.default_redirect}";')

    lines.append(f"}}")

    # Optional _error subroutine for synthetic responses
    if vc.custom_vcl_error:
        error_name = _vcl_sub_name(vc.vcl_snippet, "error")
        lines.append(f"")
        lines.append(f"sub {error_name} {{")
        for custom_line in vc.custom_vcl_error.strip("\n").splitlines():
            lines.append(custom_line.rstrip())
        lines.append(f"}}")

    lines.append(f"")
    return "\n".join(lines)


def generate_main_vcl(recv_snippets: list[str], error_snippets: list[str]) -> str:
    """Generate vcl/main.vcl with dynamic call statements."""
    lines: list[str] = []
    lines.append("# Generated by generate-tfvars.py — do not edit manually")
    lines.append("")

    for s in recv_snippets:
        lines.append(f'include "{s}";')

    if recv_snippets:
        lines.append("")

    lines.append("sub vcl_recv {")
    lines.append("  #FASTLY recv")
    lines.append("")

    for s in recv_snippets:
        lines.append(f"  call {_vcl_sub_name(s, 'recv')};")

    lines.append("")
    lines.append("  # ── GitLab Pages proxy (dictionary-based) ──")
    lines.append("  declare local var.pages_path STRING;")
    lines.append('  set var.pages_path = table.lookup(domain_to_path, req.http.Host, "");')
    lines.append("")
    lines.append('  if (var.pages_path != "") {')
    lines.append("    set req.backend = F_gitlab_pages;")
    lines.append("    set req.http.X-Orig-Host = req.http.Host;")
    lines.append('    set req.http.Host = table.lookup(domain_to_origin_host, req.http.X-Orig-Host, "");')
    lines.append('    set req.url = regsub(var.pages_path, "/$", "") req.url;')
    lines.append('    unset req.http.X-Forwarded-Host;')
    lines.append("    return(lookup);")
    lines.append("  }")
    lines.append("")
    lines.append("  # ── Simple redirects (dictionary-based) ──")
    lines.append("  declare local var.redirect_target STRING;")
    lines.append('  set var.redirect_target = table.lookup(simple_redirects, req.http.Host, "");')
    lines.append("")
    lines.append('  if (var.redirect_target != "") {')
    lines.append("    declare local var.preserve_path STRING;")
    lines.append('    set var.preserve_path = table.lookup(redirect_preserve_path, req.http.Host, "false");')
    lines.append("")
    lines.append('    if (var.preserve_path == "true") {')
    lines.append("      error 751 var.redirect_target req.url;")
    lines.append("    } else {")
    lines.append("      error 751 var.redirect_target;")
    lines.append("    }")
    lines.append("  }")
    lines.append("")
    lines.append('  error 404 "Not Found";')
    lines.append("}")
    lines.append("")

    lines.append("sub vcl_fetch {")
    lines.append("  #FASTLY fetch")
    lines.append("")
    lines.append("  if (req.http.X-Orig-Host && beresp.http.Location) {")
    lines.append("    set beresp.http.Location = regsub(")
    lines.append("      beresp.http.Location,")
    lines.append(r'      "^(https?:)?//[^/]+\.pages\.gitlab\.gnome\.org/[^/]+/[^/]+/",')
    lines.append('      "https://" req.http.X-Orig-Host "/"')
    lines.append("    );")
    lines.append("  }")
    lines.append("")
    lines.append("  return(deliver);")
    lines.append("}")
    lines.append("")

    lines.append("sub vcl_deliver {")
    lines.append("  #FASTLY deliver")
    lines.append("")
    lines.append("  unset resp.http.X-Orig-Host;")
    lines.append("")
    lines.append("  return(deliver);")
    lines.append("}")
    lines.append("")

    lines.append("sub vcl_error {")
    lines.append("  #FASTLY error")
    lines.append("")
    lines.append("  # 301 permanent redirect")
    lines.append("  if (obj.status == 751) {")
    lines.append("    set obj.status = 301;")
    lines.append("    set obj.http.Location = obj.response;")
    lines.append('    set obj.http.Content-Type = "text/html; charset=utf-8";')
    lines.append('    synthetic {"<html><body>Moved permanently to <a href=""} obj.response {"">here</a>.</body></html>"};')
    lines.append("    return(deliver);")
    lines.append("  }")
    lines.append("")

    for s in error_snippets:
        lines.append(f"  call {_vcl_sub_name(s, 'error')};")

    lines.append("")
    lines.append("  return(deliver);")
    lines.append("}")
    lines.append("")

    lines.append("sub vcl_hash {")
    lines.append("  #FASTLY hash")
    lines.append("")
    lines.append("  set req.hash += req.http.Host;")
    lines.append("  set req.hash += req.url;")
    lines.append("")
    lines.append("  return(hash);")
    lines.append("}")
    lines.append("")

    return "\n".join(lines)


# ── Validation ──

def validate(
    domain_to_path: dict[str, str],
    simple_redirects: dict[str, str],
    all_domains: list[str],
    vcl_configs: list[VclConfig],
) -> list[str]:
    """Check for duplicate domains, invalid paths, and TOML consistency."""
    errors: list[str] = []

    seen: dict[str, str] = {}
    for domain in all_domains:
        if domain in seen:
            errors.append(f"Duplicate domain: {domain} (first seen in {seen[domain]})")
        if domain in domain_to_path:
            seen[domain] = "proxy"
        elif domain in simple_redirects:
            seen[domain] = "redirect"
        else:
            seen[domain] = "vcl-only"

    for domain, path in domain_to_path.items():
        if not path.startswith("/"):
            errors.append(f"Proxy path for {domain} must start with /: {path}")

    for domain, target in simple_redirects.items():
        if not target.startswith("http"):
            errors.append(f"Redirect target for {domain} must be a URL: {target}")

    snippet_names: dict[str, str] = {}
    for vc in vcl_configs:
        if vc.vcl_snippet in snippet_names:
            errors.append(
                f"Duplicate vcl_snippet '{vc.vcl_snippet}': "
                f"used by {snippet_names[vc.vcl_snippet]} and {vc.domain}"
            )
        snippet_names[vc.vcl_snippet] = vc.domain

        for path, target in vc.path_redirects.items():
            if not path.startswith("/"):
                errors.append(f"{vc.domain}: path_redirect key must start with /: {path}")
            if not target.startswith("http"):
                errors.append(f"{vc.domain}: path_redirect target must be a URL: {target}")
        for subpath, target_path in vc.subpath_proxies.items():
            if not subpath.startswith("/"):
                errors.append(f"{vc.domain}: subpath_proxy key must start with /: {subpath}")
            if not target_path.startswith("/"):
                errors.append(f"{vc.domain}: subpath_proxy target must start with /: {target_path}")
        if vc.default_redirect and not vc.default_redirect.startswith("http"):
            errors.append(f"{vc.domain}: default_redirect must be a URL: {vc.default_redirect}")

    return errors


# ── TLS Group Generation ──

def build_tls_groups(all_domains: list[str]) -> dict[str, list[str]]:
    """Build TLS certificate groups using wildcards where beneficial.

    Groups:
      - gnome_org: gnome.org apex + wildcards for subdomains
      - guadec_org: wildcard for *.guadec.org
      - gtk_org: wildcard for *.gtk.org
      - other: remaining domains (wildcards if 2+ siblings share a parent)
    """
    gnome_org_domains: list[str] = []
    guadec_org_domains: list[str] = []
    gtk_org_domains: list[str] = []
    other_domains: list[str] = []

    for domain in all_domains:
        if domain == "gnome.org" or domain.endswith(".gnome.org"):
            gnome_org_domains.append(domain)
        elif domain == "guadec.org" or domain.endswith(".guadec.org"):
            guadec_org_domains.append(domain)
        elif domain == "gtk.org" or domain.endswith(".gtk.org"):
            gtk_org_domains.append(domain)
        else:
            other_domains.append(domain)

    return {
        "gnome_org": _build_sans(gnome_org_domains),
        "guadec_org": _build_sans(guadec_org_domains),
        "gtk_org": _build_sans(gtk_org_domains),
        "other": _build_sans(other_domains),
    }


def _build_sans(domains: list[str]) -> list[str]:
    """Convert a list of domains into an optimized SAN list using wildcards."""
    from collections import defaultdict

    # Group subdomains by their immediate parent
    # e.g. "apps.gnome.org" -> parent "gnome.org", "icons.design.gnome.org" -> parent "design.gnome.org"
    parent_children: defaultdict[str, list[str]] = defaultdict(list)
    apex_domains: list[str] = []

    for domain in domains:
        parts = domain.split(".")
        if len(parts) <= 2:
            # Apex domain (e.g. "gnome.org", "gtk.org")
            apex_domains.append(domain)
        else:
            # Subdomain — parent is everything after the first label
            parent = ".".join(parts[1:])
            parent_children[parent].append(domain)

    sans: set[str] = set()

    # Always include apex domains explicitly
    for apex in apex_domains:
        sans.add(apex)

    # For each parent, decide wildcard vs explicit
    for parent, children in parent_children.items():
        if len(children) >= 2:
            sans.add(f"*.{parent}")
            sans.add(parent)
        else:
            for child in children:
                sans.add(child)

    # Remove domains already covered by a wildcard in the same set
    wildcards = [s for s in sans if s.startswith("*.")]
    redundant = set()
    for san in list(sans):
        if san.startswith("*."):
            continue
        parts = san.split(".")
        if len(parts) > 2:
            parent = ".".join(parts[1:])
            if f"*.{parent}" in sans:
                redundant.add(san)
    sans -= redundant

    return sorted(sans)


# ── Main ──

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate terraform.tfvars.json and VCL from TOML configs"
    )
    parser.add_argument("--check", action="store_true", help="Validate only, don't write output")
    parser.add_argument("--output", default="terraform.tfvars.json", help="Output file path")
    args = parser.parse_args()

    domain_to_path, domain_to_origin_host, proxy_domains, proxy_vcl = parse_proxy_configs()
    simple_redirects, preserve_path, redirect_domains, redirect_vcl = parse_redirect_configs()

    all_domains = sorted(set(proxy_domains + redirect_domains))
    all_vcl = proxy_vcl + redirect_vcl

    errors = validate(domain_to_path, simple_redirects, all_domains, all_vcl)
    if errors:
        print("Validation errors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)

    vcl_files: dict[str, str] = {}
    for vc in all_vcl:
        content = generate_vcl(vc)
        if content:
            vcl_files[vc.vcl_snippet] = content

    print(f"Total domains:    {len(all_domains)}")
    print(f"  Dictionary (proxy):    {len(domain_to_path)}")
    print(f"  Dictionary (redirect): {len(simple_redirects)}")
    print(f"  VCL-only:              {len(all_domains) - len(domain_to_path) - len(simple_redirects)}")
    print(f"Generated VCL:    {len(vcl_files)} ({', '.join(sorted(vcl_files.keys()))})")

    if args.check:
        print("Validation passed.")
        return

    all_vcl_content = "\n".join(vcl_files.values())
    backends: dict[str, dict[str, object]] = {}
    if domain_to_path or "F_gitlab_pages" in all_vcl_content:
        backends["gitlab_pages"] = {"address": "production-gitlab-pages.pages.gitlab.gnome.org"}
    if "F_blogs_gnome_org" in all_vcl_content:
        backends["blogs_gnome_org"] = {"address": "blogs.gnome.org", "max_conn": 50}
    if "F_gnome_os_download" in all_vcl_content:
        backends["gnome_os_download"] = {"address": "gnome-os-download.apps.openshift.gnome.org"}

    tls_groups = build_tls_groups(all_domains)
    tls_sans_total = sum(len(v) for v in tls_groups.values())
    print(f"TLS groups:       {len(tls_groups)} ({tls_sans_total} SANs total)")
    for group_name, sans in sorted(tls_groups.items()):
        print(f"  {group_name}: {sans}")

    tfvars: dict[str, object] = {
        "domain_to_path": domain_to_path,
        "domain_to_origin_host": domain_to_origin_host,
        "simple_redirects": simple_redirects,
        "redirect_preserve_path": preserve_path,
        "all_domains": all_domains,
        "vcl_snippets": sorted(vcl_files.keys()),
        "backends": backends,
        "tls_groups": tls_groups,
    }

    output_path = Path(args.output)
    with open(output_path, "w") as fp:
        json.dump(tfvars, fp, indent=2, sort_keys=True)
        fp.write("\n")
    print(f"Wrote {output_path}")

    for snippet_name, content in vcl_files.items():
        vcl_path = VCL_DIR / f"{snippet_name}.vcl"
        with open(vcl_path, "w") as fp:
            fp.write(content)
        print(f"Wrote {vcl_path}")

    error_snippets = [vc.vcl_snippet for vc in all_vcl if vc.custom_vcl_error]
    main_vcl = generate_main_vcl(sorted(vcl_files.keys()), sorted(error_snippets))
    main_vcl_path = VCL_DIR / "main.vcl"
    with open(main_vcl_path, "w") as fp:
        fp.write(main_vcl)
    print(f"Wrote {main_vcl_path}")


if __name__ == "__main__":
    main()
