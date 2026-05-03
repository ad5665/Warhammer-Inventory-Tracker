variable "app_name" {
  description = "Name prefix for Lightsail resources."
  type        = string
  default     = "wh40k-stock-tracker"

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9-]{1,52}[a-zA-Z0-9]$", var.app_name))
    error_message = "app_name must be 3-54 characters and contain only letters, numbers, and hyphens."
  }
}

variable "aws_region" {
  description = "AWS region for the Lightsail instance."
  type        = string
  default     = "eu-west-2"
}

variable "availability_zone_suffix" {
  description = "Availability zone suffix appended to aws_region."
  type        = string
  default     = "a"

  validation {
    condition     = can(regex("^[a-z]$", var.availability_zone_suffix))
    error_message = "availability_zone_suffix must be a single lowercase letter, for example a."
  }
}

variable "blueprint_id" {
  description = "Lightsail OS blueprint ID. Check active values with aws lightsail get-blueprints."
  type        = string
  default     = "ubuntu_24_04"
}

variable "bundle_id" {
  description = "Lightsail instance bundle. micro_3_0 is the low-cost 1 GB RAM plan."
  type        = string
  default     = "micro_3_0"
}

variable "ip_address_type" {
  description = "Lightsail IP mode."
  type        = string
  default     = "ipv4"

  validation {
    condition     = contains(["ipv4", "dualstack"], var.ip_address_type)
    error_message = "ip_address_type must be ipv4 or dualstack. This stack creates an IPv4 static IP."
  }
}

variable "ssh_public_key_path" {
  description = "Path to the public SSH key to import into Lightsail."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_cidrs" {
  description = "IPv4 CIDR ranges allowed to SSH to the instance. Replace the default with your public IP /32."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "ssh_ipv6_cidrs" {
  description = "IPv6 CIDR ranges allowed to SSH to the instance."
  type        = list(string)
  default     = []
}

variable "web_cidrs" {
  description = "IPv4 CIDR ranges allowed to reach HTTP and HTTPS."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "web_ipv6_cidrs" {
  description = "IPv6 CIDR ranges allowed to reach HTTP and HTTPS."
  type        = list(string)
  default     = []
}

variable "container_image" {
  description = "Container image to run on the Lightsail instance."
  type        = string
  default     = "ad5665/warhammer-inventory-tracker:latest"
}

variable "auth_enabled" {
  description = "Enable the application's username/password auth."
  type        = bool
  default     = true
}

variable "admin_username" {
  description = "Initial admin username. The app generates the first temporary password in container logs."
  type        = string
  default     = "admin"

  validation {
    condition     = can(regex("^[a-zA-Z0-9_.-]{3,40}$", var.admin_username))
    error_message = "admin_username must be 3-40 characters and use letters, numbers, dots, hyphens, or underscores."
  }
}

variable "session_days" {
  description = "Login session lifetime in days."
  type        = number
  default     = 30

  validation {
    condition     = var.session_days >= 1 && var.session_days == floor(var.session_days)
    error_message = "session_days must be an integer of at least 1."
  }
}

variable "cookie_secure" {
  description = "Set WH40K_COOKIE_SECURE. Defaults to true when domain_name is set, false for bare HTTP/IP testing."
  type        = bool
  default     = null
  nullable    = true
}

variable "domain_name" {
  description = "Optional hostname for Caddy and Route 53, for example inventory.example.com. Leave empty for HTTP by static IP."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Optional Route 53 hosted zone ID. When set with domain_name, Terraform creates an A record."
  type        = string
  default     = null
}

variable "backup_retention_days" {
  description = "Days of local app backup archives to keep on the instance."
  type        = number
  default     = 14

  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days == floor(var.backup_retention_days)
    error_message = "backup_retention_days must be an integer of at least 1."
  }
}

variable "enable_auto_snapshot" {
  description = "Enable Lightsail automatic instance snapshots. This improves recoverability but adds snapshot storage cost."
  type        = bool
  default     = false
}

variable "auto_snapshot_time" {
  description = "UTC hour for Lightsail automatic snapshots when enable_auto_snapshot is true."
  type        = string
  default     = "04:00"

  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):00$", var.auto_snapshot_time))
    error_message = "auto_snapshot_time must be an hourly UTC time in HH:00 format."
  }
}

variable "tags" {
  description = "Extra tags to apply to supported AWS resources."
  type        = map(string)
  default     = {}
}
