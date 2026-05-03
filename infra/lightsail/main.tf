locals {
  availability_zone       = "${var.aws_region}${var.availability_zone_suffix}"
  domain_name             = trimspace(var.domain_name)
  route53_zone_id         = var.route53_zone_id == null ? "" : trimspace(var.route53_zone_id)
  caddy_site_address      = local.domain_name != "" ? local.domain_name : ":80"
  effective_cookie_secure = var.cookie_secure == null ? local.domain_name != "" : var.cookie_secure

  tags = merge(
    {
      Application = "wh40k-stock-tracker"
      ManagedBy   = "terraform"
    },
    var.tags
  )
}

resource "aws_lightsail_key_pair" "app" {
  name       = "${var.app_name}-ssh"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "aws_lightsail_instance" "app" {
  name              = var.app_name
  availability_zone = local.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id
  ip_address_type   = var.ip_address_type
  key_pair_name     = aws_lightsail_key_pair.app.name
  user_data = templatefile("${path.module}/templates/cloud-init.sh.tftpl", {
    admin_username        = var.admin_username
    auth_enabled          = var.auth_enabled
    backup_retention_days = var.backup_retention_days
    caddy_site_address    = local.caddy_site_address
    container_image       = var.container_image
    cookie_secure         = local.effective_cookie_secure
    session_days          = var.session_days
  })

  dynamic "add_on" {
    for_each = var.enable_auto_snapshot ? [1] : []

    content {
      type          = "AutoSnapshot"
      snapshot_time = var.auto_snapshot_time
      status        = "Enabled"
    }
  }

  tags = local.tags
}

resource "aws_lightsail_static_ip" "app" {
  name = "${var.app_name}-ip"
}

resource "aws_lightsail_static_ip_attachment" "app" {
  static_ip_name = aws_lightsail_static_ip.app.name
  instance_name  = aws_lightsail_instance.app.name
}

resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name

  port_info {
    protocol   = "tcp"
    from_port  = 80
    to_port    = 80
    cidrs      = var.web_cidrs
    ipv6_cidrs = var.web_ipv6_cidrs
  }

  port_info {
    protocol   = "tcp"
    from_port  = 443
    to_port    = 443
    cidrs      = var.web_cidrs
    ipv6_cidrs = var.web_ipv6_cidrs
  }

  dynamic "port_info" {
    for_each = length(var.ssh_cidrs) > 0 || length(var.ssh_ipv6_cidrs) > 0 ? [1] : []

    content {
      protocol   = "tcp"
      from_port  = 22
      to_port    = 22
      cidrs      = var.ssh_cidrs
      ipv6_cidrs = var.ssh_ipv6_cidrs
    }
  }

  lifecycle {
    replace_triggered_by = [aws_lightsail_instance.app]
  }
}

resource "aws_route53_record" "app" {
  count = local.route53_zone_id != "" && local.domain_name != "" ? 1 : 0

  zone_id = local.route53_zone_id
  name    = local.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_lightsail_static_ip.app.ip_address]
}
