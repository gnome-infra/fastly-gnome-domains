output "service_id" {
  description = "The Fastly service ID"
  value       = fastly_service_vcl.gnome_domains.id
}

output "active_version" {
  description = "The currently active service version"
  value       = fastly_service_vcl.gnome_domains.active_version
}

output "domain_count" {
  description = "Number of domains registered on the service"
  value       = length(var.all_domains)
}

output "proxy_domain_count" {
  description = "Number of GitLab Pages proxy entries in the dictionary"
  value       = length(var.domain_to_path)
}

output "redirect_domain_count" {
  description = "Number of simple redirect entries in the dictionary"
  value       = length(var.simple_redirects)
}
