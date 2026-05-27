variable "service_name" {
  description = "Name of the Fastly service"
  type        = string
  default     = "gnome-domains"
}

variable "domain_to_path" {
  description = "Map of domain → GitLab Pages path prefix (populated from TOML by generate-tfvars.py)"
  type        = map(string)
}

variable "domain_to_origin_host" {
  description = "Map of domain → GitLab Pages origin hostname (populated from TOML by generate-tfvars.py)"
  type        = map(string)
  default     = {}
}

variable "simple_redirects" {
  description = "Map of domain → redirect target URL (populated from TOML by generate-tfvars.py)"
  type        = map(string)
}

variable "redirect_preserve_path" {
  description = "Map of domain → 'true'/'false' for whether to append request path to redirect target"
  type        = map(string)
}

variable "all_domains" {
  description = "List of all domains to register on the Fastly service (populated from TOML by generate-tfvars.py)"
  type        = list(string)
}

variable "vcl_snippets" {
  description = "List of generated VCL snippet names (populated from TOML by generate-tfvars.py)"
  type        = list(string)
  default     = []
}

variable "backends" {
  description = "Map of backend name → config, populated by generate-tfvars.py based on which backends the VCL actually references"
  type        = map(object({
    address  = string
    max_conn = optional(number, 200)
  }))
  default = {}
}

variable "tls_groups" {
  description = "Map of TLS group name → list of SANs (including wildcards) sharing a certificate, populated by generate-tfvars.py"
  type        = map(list(string))
  default     = {}
}
