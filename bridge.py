"""
MCP Server with HTTP Streamable transport for N8N
Based on modern MCP specification (post-SSE deprecation)
Fully dynamic - no hardcoded tool mappings - uses exact MCPO tool names
"""
import asyncio
import json
import logging
import os
import uuid
import time
import traceback
import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MCPO_BASE = os.getenv("MCPO_BASE", "http://mcpo:8000")

app = FastAPI(title="MCP Server for N8N (HTTP Streamable)")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store active sessions
active_sessions = {}

async def discover_servers_from_mcpo():
    """Discover all available servers directly from MCPO's root OpenAPI spec"""
    logger.info("Discovering servers from MCPO...")
    
    async with aiohttp.ClientSession() as session:
        try:
            # Get MCPO's root OpenAPI spec
            async with session.get(f"{MCPO_BASE}/openapi.json", timeout=10) as resp:
                if resp.status == 200:
                    spec = await resp.json()
                    
                    # Extract server names from the description
                    description = spec.get("info", {}).get("description", "")
                    logger.info(f"MCPO description: {description}")
                    
                    # Parse server names from lines like "- [memory](/memory/docs)"
                    import re
                    server_pattern = r'- \[([^\]]+)\]\(/([^/]+)/docs\)'
                    matches = re.findall(server_pattern, description)
                    
                    servers = []
                    for display_name, server_name in matches:
                        servers.append(server_name)
                        logger.info(f"Discovered server: {server_name} ({display_name})")
                    
                    if servers:
                        logger.info(f"Total servers discovered from MCPO: {len(servers)}")
                        return servers
                    else:
                        logger.warning("No servers found in MCPO description, trying fallback...")
                        
        except Exception as e:
            logger.error(f"Failed to discover servers from MCPO: {e}")
        
        # Fallback: Try brute force discovery
        logger.info("Attempting brute force server discovery...")
        potential_servers = [
            "time", "filesystem", "memory", "python_executor", 
            "web-content-fetcher", "sequential-thinking"
        ]
        
        discovered_servers = []
        for server in potential_servers:
            try:
                async with session.get(f"{MCPO_BASE}/{server}/openapi.json", timeout=3) as resp:
                    if resp.status == 200:
                        discovered_servers.append(server)
                        logger.info(f"Brute force discovered: {server}")
            except Exception as e:
                logger.debug(f"Server {server} not found: {e}")
        
        if discovered_servers:
            logger.info(f"Brute force discovered {len(discovered_servers)} servers")
            return discovered_servers
    
    # Final fallback
    logger.warning("Using hardcoded fallback server list")
    return ["time", "filesystem", "memory", "python_executor"]

async def load_servers_from_config():
    """Load server list from MCPO instead of config.json"""
    return await discover_servers_from_mcpo()

# Global variable to cache servers
_servers_cache = None
_servers_cache_time = 0

async def get_servers():
    """Get servers with caching"""
    global _servers_cache, _servers_cache_time
    
    current_time = time.time()
    # Cache for 1 minute (shorter than tools cache since config might change)
    if _servers_cache is None or (current_time - _servers_cache_time) > 60:
        logger.info("Refreshing servers cache from MCPO")
        _servers_cache = await load_servers_from_config()
        _servers_cache_time = current_time
    
    return _servers_cache

def get_ai_friendly_tool_info(server: str, tool_name: str, operation: dict) -> dict:
    """Use exact tool names and descriptions from OpenAPI spec"""
    
    # Use description from OpenAPI operation, with fallback chain
    description = (
        operation.get('description') or 
        operation.get('summary') or 
        f"Execute {tool_name.replace('_', ' ')} operation"
    )
    
    # Clean up description - ensure it starts with capital
    description = description.strip()
    if description and not description[0].isupper():
        description = description[0].upper() + description[1:]
    if description.endswith('.'):
        description = description[:-1]
    
    logger.info(f"Using exact tool name: {tool_name} - {description}")
    
    return {
        "name": tool_name,  # Use exact MCPO name
        "description": description
    }

def generate_property_description(prop_name: str, prop_type: str) -> str:
    """Generate descriptive text for a property based on its name and type"""
    
    # Common property name patterns and their descriptions
    name_patterns = {
        'url': 'The URL to process',
        'path': 'The file or directory path',
        'file': 'The file path or filename',
        'code': 'The code to execute',
        'content': 'The content or text data',
        'query': 'The search query or filter',
        'timeout': 'Timeout duration in milliseconds',
        'wait_time': 'Wait time in milliseconds',
        'wait_for_selector': 'CSS selector to wait for',
        'timezone': 'The timezone identifier',
        'time': 'The time value',
        'source': 'The source location or value',
        'target': 'The target location or value',
        'data': 'The data to process',
        'input': 'The input value',
        'output': 'The output destination',
        'name': 'The name identifier',
        'id': 'The unique identifier',
        'type': 'The type or category',
        'format': 'The format specification',
        'encoding': 'The encoding method',
        'size': 'The size value',
        'limit': 'The maximum limit',
        'offset': 'The offset value',
        'recursive': 'Whether to process recursively',
        'force': 'Whether to force the operation',
        'verbose': 'Whether to show detailed output'
    }
    
    # Check for exact matches first
    prop_lower = prop_name.lower()
    if prop_lower in name_patterns:
        return name_patterns[prop_lower]
    
    # Check for partial matches
    for pattern, description in name_patterns.items():
        if pattern in prop_lower:
            return description
    
    # Generate description based on type and name
    if prop_type == "boolean":
        return f"Whether to {prop_name.replace('_', ' ')}"
    elif prop_type == "integer":
        return f"The {prop_name.replace('_', ' ')} number"
    elif prop_type == "array":
        return f"List of {prop_name.replace('_', ' ')} items"
    else:
        return f"The {prop_name.replace('_', ' ')} parameter"

def improve_schema_descriptions(schema: dict, server: str, tool_name: str) -> dict:
    """Improve OpenAPI schemas dynamically without hardcoding"""
    
    if not isinstance(schema, dict):
        logger.warning(f"Invalid schema type for {tool_name}: {type(schema)}")
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False
        }
    
    # Ensure we have an object schema
    if schema.get("type") != "object":
        logger.info(f"Wrapping non-object schema for {tool_name}")
        return {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": f"Input for {tool_name.replace('_', ' ')}"
                }
            },
            "required": ["input"]
        }
    
    # Work with the existing object schema
    improved_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False
    }
    
    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])
    
    if not properties:
        logger.info(f"No properties found for {tool_name}, creating empty schema")
        return improved_schema
    
    # Process each property, improving descriptions where needed
    for prop_name, prop_def in properties.items():
        improved_prop = dict(prop_def)  # Copy original property definition
        
        # Enhance description if it's generic or missing
        current_desc = improved_prop.get("description", "")
        if not current_desc or len(current_desc) < 10:
            # Generate a better description based on property name
            improved_desc = generate_property_description(prop_name, improved_prop.get("type", "string"))
            improved_prop["description"] = improved_desc
            logger.info(f"Enhanced description for {tool_name}.{prop_name}: {improved_desc}")
        
        improved_schema["properties"][prop_name] = improved_prop
    
    # Preserve required fields
    if required_fields:
        improved_schema["required"] = required_fields
    
    logger.info(f"Processed schema for {tool_name}: {len(properties)} properties, {len(required_fields)} required")
    return improved_schema

def transform_arguments_with_schema(arguments: dict, schema: dict, tool_name: str) -> dict:
    """Transform arguments based on the actual OpenAPI schema - now fully dynamic"""
    if not schema or schema.get("type") != "object":
        logger.warning(f"Invalid schema for transformation: {tool_name}")
        return arguments
    
    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])
    
    logger.info(f"Schema properties for {tool_name}: {list(properties.keys())}")
    logger.info(f"Required fields: {required_fields}")
    
    transformed = {}
    
    # Smart parameter mapping for generic 'input' parameter
    if "input" in arguments and len(arguments) == 1:
        input_value = arguments["input"]
        
        # Priority order for mapping generic input
        mapping_priority = ["url", "path", "file", "code", "query", "content", "data"]
        
        # Try required fields first
        if required_fields:
            primary_field = required_fields[0]
            transformed[primary_field] = input_value
            logger.info(f"Mapped 'input' to required field '{primary_field}'")
        else:
            # Try priority mapping
            mapped = False
            for priority_field in mapping_priority:
                if priority_field in properties:
                    transformed[priority_field] = input_value
                    logger.info(f"Mapped 'input' to priority field '{priority_field}'")
                    mapped = True
                    break
            
            # Fallback to first available property
            if not mapped and properties:
                first_prop = list(properties.keys())[0]
                transformed[first_prop] = input_value
                logger.info(f"Mapped 'input' to first property '{first_prop}'")
    else:
        # Direct mapping for other cases
        transformed.update(arguments)
    
    # Fill in default values for missing required fields
    for field in required_fields:
        if field not in transformed:
            prop_schema = properties.get(field, {})
            default_value = prop_schema.get("default")
            
            if default_value is not None:
                transformed[field] = default_value
                logger.info(f"Added default value for '{field}': {default_value}")
            else:
                # Type-based defaults
                prop_type = prop_schema.get("type", "string")
                if prop_type == "string":
                    transformed[field] = ""
                elif prop_type == "array":
                    transformed[field] = []
                elif prop_type == "boolean":
                    transformed[field] = False
                elif prop_type == "integer":
                    transformed[field] = 0
                else:
                    transformed[field] = ""
                logger.info(f"Added default for required field '{field}' ({prop_type})")
    
    return transformed

# Also add this function to test individual tool calls
@app.post("/test_tool")
async def test_tool_call():
    """Test endpoint to manually call a tool"""
    try:
        # Test the run_script tool specifically
        test_request = {
            "jsonrpc": "2.0",
            "id": "test123",
            "method": "tools/call",
            "params": {
                "name": "run_script",
                "arguments": {
                    "file": "/app/sandbox/code.py"
                }
            }
        }
        
        logger.info(f"Testing tool call: {test_request}")
        
        # Simulate the request
        from fastapi import Request
        from unittest.mock import MagicMock
        
        mock_request = MagicMock()
        mock_request.body.return_value = json.dumps(test_request).encode()
        
        # Call our streamable endpoint
        response = await streamable_endpoint(mock_request)
        
        return response
        
    except Exception as e:
        return JSONResponse({
            "error": str(e),
            "traceback": traceback.format_exc()
        })

# Add this debugging endpoint to see exactly what tools/list returns
@app.get("/test_tools_list")
async def test_tools_list():
    """Test the tools/list response that N8n sees"""
    try:
        tools = await get_cached_tools()
        
        # Format exactly like the real tools/list response
        clean_tools = []
        for tool in tools:
            clean_tool = {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["inputSchema"]
            }
            clean_tools.append(clean_tool)
        
        response = {
            "jsonrpc": "2.0",
            "id": "test",
            "result": {"tools": clean_tools}
        }
        
        logger.info(f"Tools list response would be: {json.dumps(response, indent=2)}")
        return JSONResponse(response)
        
    except Exception as e:
        return JSONResponse({
            "error": str(e),
            "traceback": traceback.format_exc()
        })

async def get_all_tools():
    """Fetch all tools from MCPO servers and parse their OpenAPI schemas dynamically"""
    logger.info("=== Starting get_all_tools() ===")
    all_tools = []
    servers = await get_servers()
    logger.info(f"Using servers: {servers}")
    
    async with aiohttp.ClientSession() as session:
        for server in servers:
            logger.info(f"Processing server: {server}")
            try:
                openapi_url = f"{MCPO_BASE}/{server}/openapi.json"
                logger.info(f"Fetching OpenAPI spec from: {openapi_url}")
                
                async with session.get(openapi_url) as resp:
                    logger.info(f"OpenAPI response status for {server}: {resp.status}")
                    if resp.status == 200:
                        spec = await resp.json()
                        logger.info(f"Processing OpenAPI spec for {server}, paths: {list(spec.get('paths', {}).keys())}")
                        
                        for path, path_item in spec.get("paths", {}).items():
                            logger.info(f"  Processing path: {path}")
                            for method, operation in path_item.items():
                                if method.upper() == "POST":
                                    logger.info(f"    Found POST operation: {operation.get('operationId', 'no-id')}")
                                    
                                    # Extract the clean endpoint name and operation ID
                                    endpoint_path = path.strip("/")
                                    operation_id = operation.get("operationId", "")
                                    
                                    # Get the tool name from operation ID or path
                                    if operation_id.startswith("tool_") and operation_id.endswith("_post"):
                                        tool_name = operation_id[5:-5]  # Remove "tool_" prefix and "_post" suffix
                                        logger.info(f"    Extracted tool name from operation_id: {tool_name}")
                                    else:
                                        tool_name = endpoint_path
                                        logger.info(f"    Using endpoint path as tool name: {tool_name}")
                                    
                                    # Extract the actual parameter schema from OpenAPI spec
                                    request_body = operation.get("requestBody", {})
                                    content = request_body.get("content", {})
                                    json_content = content.get("application/json", {})
                                    schema_ref = json_content.get("schema", {})
                                    
                                    logger.info(f"    Schema ref: {schema_ref}")
                                    
                                    # Resolve schema reference if it's a $ref
                                    actual_schema = {"type": "object", "properties": {}}
                                    if "$ref" in schema_ref:
                                        # Extract schema name from $ref like "#/components/schemas/list_directory_form_model"
                                        schema_name = schema_ref["$ref"].split("/")[-1]
                                        components = spec.get("components", {})
                                        schemas = components.get("schemas", {})
                                        if schema_name in schemas:
                                            actual_schema = schemas[schema_name]
                                            logger.info(f"    Resolved schema: {actual_schema}")
                                        else:
                                            logger.warning(f"    Schema {schema_name} not found in components")
                                    elif "type" in schema_ref:
                                        actual_schema = schema_ref
                                        logger.info(f"    Using direct schema: {actual_schema}")
                                    
                                    # Ensure the schema is a valid object schema for N8N
                                    if actual_schema.get("type") != "object":
                                        # Convert non-object schemas to object wrapper
                                        actual_schema = {
                                            "type": "object",
                                            "properties": {
                                                "value": actual_schema
                                            },
                                            "required": ["value"]
                                        }
                                        logger.info(f"    Wrapped non-object schema")
                                    
                                    # Get tool info using exact name and OpenAPI description
                                    tool_info_result = get_ai_friendly_tool_info(server, tool_name, operation)
                                    
                                    # Improve the input schema with better property descriptions
                                    improved_schema = improve_schema_descriptions(actual_schema, server, tool_name)
                                    
                                    tool_info = {
                                        "name": tool_info_result["name"],
                                        "description": tool_info_result["description"],
                                        "inputSchema": improved_schema,
                                        "_server": server,
                                        "_original_name": tool_name,
                                        "_endpoint_path": endpoint_path,
                                        "_original_schema": actual_schema  # Store original for parameter mapping
                                    }
                                    
                                    all_tools.append(tool_info)
                                    logger.info(f"    Added tool: {tool_info_result['name']} -> /{endpoint_path}")
                    else:
                        logger.error(f"Failed to fetch OpenAPI for {server}: HTTP {resp.status}")
            
            except Exception as e:
                logger.error(f"Failed to get tools from {server}: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
    
    logger.info(f"=== Total tools loaded: {len(all_tools)} ===")
    return all_tools

# Cache the tools to avoid repeated API calls
_tools_cache = None
_tools_cache_time = 0

async def get_cached_tools():
    """Get tools with caching to avoid repeated OpenAPI calls"""
    global _tools_cache, _tools_cache_time
    
    current_time = time.time()
    # Cache for 5 minutes
    if _tools_cache is None or (current_time - _tools_cache_time) > 300:
        logger.info("Refreshing tools cache from OpenAPI specs")
        _tools_cache = await get_all_tools()
        _tools_cache_time = current_time
    
    return _tools_cache

# Add request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all incoming requests for debugging"""
    start_time = time.time()
    
    # Log request details
    logger.info(f">>> {request.method} {request.url}")
    logger.info(f">>> Headers: {dict(request.headers)}")
    
    if request.method == "POST":
        body = await request.body()
        if body:
            try:
                body_json = json.loads(body.decode())
                logger.info(f">>> Body: {body_json}")
            except:
                logger.info(f">>> Body (raw): {body[:200]}...")
        
        # Recreate request with body for FastAPI
        async def receive():
            return {"type": "http.request", "body": body}
        
        request._receive = receive
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    logger.info(f"<<< {response.status_code} (took {process_time:.3f}s)")
    
    return response

@app.get("/")
async def root(request: Request):
    """Root endpoint - handle both SSE requests and info requests"""
    accept_header = request.headers.get("accept", "")
    
    if "text/event-stream" in accept_header:
        logger.info("Legacy SSE connection requested")
        return await legacy_sse_endpoint()
    else:
        # Return MCP server info
        return JSONResponse({
            "jsonrpc": "2.0",
            "result": {
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "mcpo-mcp-bridge",
                    "version": "1.0.0"
                }
            }
        })

@app.get("/health")
async def health():
    servers = await get_servers()
    return JSONResponse({
        "status": "healthy", 
        "mcpo_base": MCPO_BASE,
        "servers": servers,
        "total_servers": len(servers)
    })

@app.get("/debug")
async def debug_info():
    """Debug endpoint to check bridge status"""
    try:
        servers = await get_servers()
        tools = await get_cached_tools()
        
        # Test MCPO connectivity
        mcpo_status = "unknown"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{MCPO_BASE}/health", timeout=5) as resp:
                    mcpo_status = f"HTTP {resp.status}"
        except Exception as e:
            mcpo_status = f"Error: {str(e)}"
        
        return JSONResponse({
            "bridge_status": "running",
            "mcpo_base": MCPO_BASE,
            "mcpo_connectivity": mcpo_status,
            "servers_loaded": servers,
            "total_servers": len(servers),
            "tools_loaded": len(tools),
            "tool_names": [t["name"] for t in tools],
            "active_sessions": len(active_sessions),
            "discovery_method": "Dynamic MCPO OpenAPI (Exact Names)",
            "environment": {
                "MCPO_BASE": os.getenv("MCPO_BASE", "not set")
            }
        })
    except Exception as e:
        return JSONResponse({
            "error": str(e),
            "traceback": traceback.format_exc()
        })

@app.get("/tools/debug")
async def debug_tools():
    """Debug endpoint to see exactly what tools are available"""
    try:
        tools = await get_cached_tools()
        return JSONResponse({
            "total_tools": len(tools),
            "tools": [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "server": tool["_server"],
                    "original_name": tool["_original_name"],
                    "endpoint": tool["_endpoint_path"],
                    "schema_properties": list(tool["inputSchema"].get("properties", {}).keys()) if tool["inputSchema"] else []
                }
                for tool in tools
            ]
        })
    except Exception as e:
        return JSONResponse({"error": str(e), "traceback": traceback.format_exc()})

@app.get("/sse")
async def legacy_sse_endpoint():
    """Legacy SSE endpoint for backward compatibility"""
    session_id = str(uuid.uuid4())
    
    async def event_generator():
        try:
            logger.info(f"Legacy SSE session: {session_id}")
            
            # Store session
            active_sessions[session_id] = {
                "connected": True,
                "created": asyncio.get_event_loop().time()
            }
            
            # Try multiple SSE event formats that N8N might expect
            
            # Format 1: Simple endpoint URL
            endpoint_url = f"/messages?session_id={session_id}"
            yield f"event: endpoint\ndata: {endpoint_url}\n\n"
            
            # Format 2: JSON endpoint info
            endpoint_info = json.dumps({
                "endpoint": endpoint_url,
                "session_id": session_id,
                "type": "mcp_endpoint"
            })
            yield f"event: mcp_endpoint\ndata: {endpoint_info}\n\n"
            
            # Format 3: Connection ready
            ready_info = json.dumps({"ready": True, "session_id": session_id})
            yield f"event: ready\ndata: {ready_info}\n\n"
            
            # Format 4: Simple data with URL
            yield f"data: {endpoint_url}\n\n"
            
            # Format 5: Server info as event
            server_info = json.dumps({
                "serverInfo": {
                    "name": "mcpo-mcp-bridge",
                    "version": "1.0.0"
                },
                "capabilities": {"tools": {}},
                "session_id": session_id
            })
            yield f"event: server_info\ndata: {server_info}\n\n"
            
            logger.info(f"Sent multiple SSE event formats for session {session_id}")
            
            # Keep connection alive with periodic pings
            ping_counter = 0
            while session_id in active_sessions and active_sessions[session_id].get("connected"):
                await asyncio.sleep(30)
                ping_counter += 1
                ping_data = json.dumps({"ping": ping_counter, "timestamp": asyncio.get_event_loop().time()})
                yield f"event: ping\ndata: {ping_data}\n\n"
                
        except asyncio.CancelledError:
            logger.info(f"Legacy SSE connection cancelled for session {session_id}")
        except Exception as e:
            logger.error(f"Legacy SSE error for session {session_id}: {e}")
        finally:
            if session_id in active_sessions:
                del active_sessions[session_id]
                logger.info(f"Cleaned up legacy SSE session: {session_id}")
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no"
        }
    )

@app.post("/")
async def streamable_endpoint(request: Request):
    """HTTP Streamable endpoint - modern MCP transport"""
    try:
        # Parse JSON-RPC request
        body = await request.body()
        if not body:
            return JSONResponse({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Empty request"}
            })
        
        try:
            message = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            return JSONResponse({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"}
            })
        
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params", {})
        
        logger.info(f"HTTP Streamable Request: {method} (id: {msg_id})")
        logger.info(f"Request params: {params}")
        
        # Handle different MCP methods
        if method == "initialize":
            logger.info("Handling HTTP Streamable initialize")
            
            # Extract client info
            client_info = params.get("clientInfo", {})
            protocol_version = params.get("protocolVersion", "2024-11-05")
            client_capabilities = params.get("capabilities", {})
            
            logger.info(f"Client: {client_info.get('name', 'unknown')} v{client_info.get('version', 'unknown')}")
            logger.info(f"Protocol: {protocol_version}")
            logger.info(f"Client capabilities: {client_capabilities}")
            
            response_data = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {},
                        "resources": {},
                        "prompts": {}
                    },
                    "serverInfo": {
                        "name": "mcpo-mcp-bridge",
                        "version": "1.0.0"
                    }
                }
            }
            
            return JSONResponse(response_data)
        
        elif method == "notifications/initialized":
            logger.info("HTTP Streamable initialization complete")
            # For notifications, return empty response (no id)
            return JSONResponse({"jsonrpc": "2.0"})
        
        elif method == "tools/list":
            logger.info("Listing tools via HTTP Streamable")
            tools = await get_cached_tools()
            clean_tools = []
            for tool in tools:
                clean_tool = {
                    "name": tool["name"],
                    "description": tool["description"],
                    "inputSchema": tool["inputSchema"]
                }
                clean_tools.append(clean_tool)
            
            logger.info(f"Returning {len(clean_tools)} tools")
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": clean_tools}
            })
        
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            
            logger.info("=" * 50)
            logger.info(f"TOOL CALL RECEIVED")
            logger.info(f"Tool name: {tool_name}")
            logger.info(f"Arguments: {arguments}")
            logger.info(f"Arguments type: {type(arguments)}")
            logger.info("=" * 50)
            
            # Find the tool info from our cached tools
            all_tools = await get_cached_tools()
            
            # Log all available tools for debugging
            logger.info(f"Available tools ({len(all_tools)}):")
            for i, tool in enumerate(all_tools):
                logger.info(f"  {i+1}. {tool['name']} - {tool['description'][:60]}...")
            
            tool_info = next((t for t in all_tools if t["name"] == tool_name), None)
            
            if not tool_info:
                logger.error(f"TOOL NOT FOUND: {tool_name}")
                logger.info(f"Looking for exact matches...")
                exact_matches = [t["name"] for t in all_tools if tool_name.lower() in t["name"].lower()]
                logger.info(f"Partial matches: {exact_matches}")
                
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32602, 
                        "message": f"Tool not found: {tool_name}. Available tools: {[t['name'] for t in all_tools[:5]]}"
                    }
                })
            
            logger.info(f"TOOL FOUND: {tool_info['name']}")
            logger.info(f"Server: {tool_info['_server']}")
            logger.info(f"Original name: {tool_info['_original_name']}")
            logger.info(f"Endpoint path: {tool_info['_endpoint_path']}")
            
            server_name = tool_info["_server"]
            endpoint_path = tool_info["_endpoint_path"]
            original_schema = tool_info["_original_schema"]
            
            # Transform arguments using the actual schema
            transformed_args = transform_arguments_with_schema(arguments, original_schema, tool_name)
            
            logger.info(f"PARAMETER TRANSFORMATION:")
            logger.info(f"  Original args: {arguments}")
            logger.info(f"  Transformed args: {transformed_args}")
            logger.info(f"  Schema: {original_schema}")
            
            # Construct the endpoint URL using the actual path from OpenAPI
            endpoint_url = f"{MCPO_BASE}/{server_name}/{endpoint_path}"
            logger.info(f"CALLING MCPO: {endpoint_url}")
            
            # Execute tool via MCPO
            async with aiohttp.ClientSession() as session:
                try:
                    logger.info(f"Sending POST to {endpoint_url} with data: {transformed_args}")
                    async with session.post(endpoint_url, json=transformed_args, timeout=300) as resp:
                        response_text = await resp.text()
                        logger.info(f"MCPO Response: Status {resp.status}")
                        logger.info(f"MCPO Response Body: {response_text[:500]}...")
                        
                        if resp.status == 200:
                            try:
                                result = json.loads(response_text)
                            except json.JSONDecodeError:
                                result = response_text
                            
                            # Format as MCP expects
                            if isinstance(result, (dict, list)):
                                content_text = json.dumps(result, indent=2)
                            else:
                                content_text = str(result)
                            
                            logger.info(f"SUCCESS: Returning result to N8N")
                            return JSONResponse({
                                "jsonrpc": "2.0",
                                "id": msg_id,
                                "result": {
                                    "content": [
                                        {"type": "text", "text": content_text}
                                    ]
                                }
                            })
                        else:
                            logger.error(f"MCPO ERROR {resp.status}: {response_text}")
                            return JSONResponse({
                                "jsonrpc": "2.0",
                                "id": msg_id,
                                "error": {"code": -32603, "message": f"Tool execution failed (HTTP {resp.status}): {response_text}"}
                            })
                except Exception as e:
                    logger.error(f"TOOL EXECUTION EXCEPTION: {e}")
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    return JSONResponse({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": f"Tool execution error: {str(e)}"}
                    })
        
        else:
            logger.warning(f"Unknown method: {method}")
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            })
    
    except Exception as e:
        logger.error(f"HTTP Streamable error: {e}")
        return JSONResponse({
            "jsonrpc": "2.0",
            "error": {"code": -32603, "message": f"Internal error: {str(e)}"}
        })

@app.post("/messages")
async def handle_legacy_messages(request: Request):
    """Handle legacy SSE-based MCP messages"""
    try:
        # Get session ID from query parameters
        session_id = request.query_params.get("session_id")
        if not session_id:
            return JSONResponse({
                "jsonrpc": "2.0",
                "error": {"code": -32602, "message": "Missing session_id"}
            })
        
        # Validate session exists
        if session_id not in active_sessions:
            logger.warning(f"Invalid session ID: {session_id}")
            return JSONResponse({
                "jsonrpc": "2.0", 
                "error": {"code": -32602, "message": "Invalid session"}
            })
        
        # Parse request body
        body = await request.body()
        if not body:
            return JSONResponse({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Empty request"}
            })
        
        try:
            message = json.loads(body.decode('utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            return JSONResponse({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"}
            })
        
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params", {})
        
        logger.info(f"Legacy MCP Request: {method} (session: {session_id}, id: {msg_id})")
        
        if method == "initialize":
            logger.info("Handling legacy MCP initialize")
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "mcpo-mcp-bridge",
                        "version": "1.0.0"
                    }
                }
            })
        
        elif method == "notifications/initialized":
            logger.info("Legacy MCP initialization complete")
            return JSONResponse({"jsonrpc": "2.0"})
        
        elif method == "tools/list":
            logger.info("Listing legacy MCP tools")
            tools = await get_cached_tools()
            clean_tools = []
            for tool in tools:
                clean_tool = {
                    "name": tool["name"],
                    "description": tool["description"],
                    "inputSchema": tool["inputSchema"]
                }
                clean_tools.append(clean_tool)
            
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": clean_tools}
            })
        
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            
            logger.info("=" * 50)
            logger.info(f"LEGACY TOOL CALL RECEIVED")
            logger.info(f"Tool name: {tool_name}")
            logger.info(f"Arguments: {arguments}")
            logger.info("=" * 50)
            
            # Find the tool info from our cached tools
            all_tools = await get_cached_tools()
            tool_info = next((t for t in all_tools if t["name"] == tool_name), None)
            
            if not tool_info:
                logger.error(f"LEGACY TOOL NOT FOUND: {tool_name}")
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": f"Tool not found: {tool_name}"}
                })
            
            server_name = tool_info["_server"]
            endpoint_path = tool_info["_endpoint_path"]
            original_schema = tool_info["_original_schema"]
            
            # Get available servers to validate
            available_servers = await get_servers()
            if server_name not in available_servers:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": f"Unknown server: {server_name}"}
                })
            
            # Transform arguments using the same logic as HTTP Streamable
            transformed_args = transform_arguments_with_schema(arguments, original_schema, tool_name)
            
            logger.info(f"LEGACY PARAMETER TRANSFORMATION:")
            logger.info(f"  Original args: {arguments}")
            logger.info(f"  Transformed args: {transformed_args}")
            
            # Execute tool via MCPO using the endpoint path
            async with aiohttp.ClientSession() as session:
                endpoint_url = f"{MCPO_BASE}/{server_name}/{endpoint_path}"
                logger.info(f"LEGACY CALLING MCPO: {endpoint_url}")
                
                try:
                    async with session.post(endpoint_url, json=transformed_args, timeout=300) as resp:
                        response_text = await resp.text()
                        logger.info(f"LEGACY MCPO Response: Status {resp.status}")
                        
                        if resp.status == 200:
                            try:
                                result = json.loads(response_text)
                            except json.JSONDecodeError:
                                result = response_text
                            
                            # Format as MCP expects
                            if isinstance(result, (dict, list)):
                                content_text = json.dumps(result, indent=2)
                            else:
                                content_text = str(result)
                            
                            return JSONResponse({
                                "jsonrpc": "2.0",
                                "id": msg_id,
                                "result": {
                                    "content": [
                                        {"type": "text", "text": content_text}
                                    ]
                                }
                            })
                        else:
                            logger.error(f"Legacy MCPO error {resp.status}: {response_text}")
                            return JSONResponse({
                                "jsonrpc": "2.0",
                                "id": msg_id,
                                "error": {"code": -32603, "message": f"Tool execution failed: {response_text}"}
                            })
                except Exception as e:
                    logger.error(f"Legacy tool execution error: {e}")
                    return JSONResponse({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": str(e)}
                    })
        
        else:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            })
    
    except Exception as e:
        logger.error(f"Legacy message handling error: {e}")
        return JSONResponse({
            "jsonrpc": "2.0",
            "error": {"code": -32603, "message": f"Internal error: {str(e)}"}
        })

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3002)
