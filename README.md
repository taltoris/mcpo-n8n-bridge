# MCPO N8N Bridge

A Docker container that bridges MCPO (Model Context Protocol server preferred for OpenWebUI) to N8N by converting MCP output into an N8N-compatible format for workflow automation.

## Overview

This bridge service takes MCP output from MCPO and transforms it into an N8N-compatible HTTP stream format, enabling seamless integration between Model Context Protocol services and N8N workflows.

## Prerequisites

- Docker and Docker Compose
- N8N Community version with tool usage enabled
- MCPO installation (instructions included below)

### N8N Configuration

You must be using the "community" version of the N8N client. If you are self-hosting N8N, enable tool usage by adding this environment variable:

```
N8N_COMMUNITY_PACKAGES_ALLOW_TOOL_USAGE=true
```

## Installation

### 1. Clone the Repository

```bash
cd ~/docker/open-webui
git clone https://github.com/taltoris/mcpo-n8n-bridge.git
cd mcpo-n8n-bridge
```

### 2. Install MCPO (if not already installed)

```bash
cd ~/docker/open-webui
git clone https://github.com/open-webui/mcpo.git
```

### 3. Directory Structure

The recommended directory structure for a unified setup:

```
~/docker/open-webui/
├── docker-compose.yml          # Unified compose file for all services
├── mcpo/                       # MCPO installation
└── mcpo-n8n-bridge/           # This bridge service
```

This structure allows you to manage open-webui, MCPO, N8N, and the bridge service together in a single Docker Compose configuration.

### 4. Setup Docker Compose

Assuming you are still in the mcpo-n8n-bridge directory:

```bash
cd ~/docker/open-webui
mv mcpo-n8n-bridge/example-docker-compose.yml docker-compose.yml
```

**Note:** You may need to adjust paths in the configuration files to match your specific setup.

## Usage

1. Start all services using Docker Compose:
   ```bash
   docker-compose up -d
   ```

2. The bridge will automatically convert MCPO output to N8N-compatible format

3. Configure your N8N workflows to consume the bridged output at the default endpoint:
   ```
   http://mcpo-n8n-bridge:3002/
   ```

4. Set up your MCP client nodes in N8N to use http-streaming mode

## Configuration

### Default Endpoint

The bridge service runs on port 3002 by default. When configuring N8N workflows, use:
```
http://mcpo-n8n-bridge:3002/
```

This endpoint provides the http-streamable MCP output that N8N can consume.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Support

For issues and questions, please use the GitHub issue tracker.
