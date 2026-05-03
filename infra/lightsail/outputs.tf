output "static_ip" {
  description = "Lightsail static IPv4 address."
  value       = aws_lightsail_static_ip.app.ip_address
}

output "app_url" {
  description = "Primary URL for the application."
  value       = local.domain_name != "" ? "https://${local.domain_name}" : "http://${aws_lightsail_static_ip.app.ip_address}"
}

output "ssh_command" {
  description = "SSH command for the Ubuntu Lightsail instance."
  value       = "ssh ubuntu@${aws_lightsail_static_ip.app.ip_address}"
}

output "admin_password_log_command" {
  description = "Command to retrieve the first generated admin password from container logs."
  value       = "ssh ubuntu@${aws_lightsail_static_ip.app.ip_address} 'sudo docker logs wh40k-stock-tracker 2>&1 | grep \"Temporary admin credentials\"'"
}

output "route53_record" {
  description = "Route 53 record created for the app, if enabled."
  value       = try(aws_route53_record.app[0].fqdn, null)
}

output "deployment_directory" {
  description = "Directory on the instance containing compose files, data, backups, and Caddy state."
  value       = "/opt/wh40k-stock-tracker"
}
