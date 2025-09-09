#!/usr/bin/env python3
"""
MCP Server with HTTP Streamable transport for N8N
Based on modern MCP specification (post-SSE deprecation)
"""
import asyncio
import json
import logging
import os
import uuid
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

async def load_servers_from_config():
    """Load server list from config.json dynamically"""
    config_path = "/app/config/config.json"
    logger.info(f"Attempting to load config from: {config_path}")
    
    try:
        # Check if file exists
        import os
        if not os.path.exists(config_path):
            logger.error(f"Config file does not exist: {config_path}")
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        logger.info(f"Config file found, reading...")
        with open(config_path, "r") as f:
            config = json.load(f)
            logger.info(f"Config loaded successfully: {config}")
            mcp_servers = config.get("mcpServers", {})
            server_names = list(mcp_servers.keys())
            logger.info(f"Loaded {len(server_names)} servers from config: {server_names}")
            return server_names
    except Exception as e:
        logger.error(f"Failed to load config.json: {e}")
        # Fallback to hardcoded servers if config loading fails
        fallback_servers = ["time", "filesystem", "memory", "python_executor"]
        logger.warning(f"Using fallback servers: {fallback_servers}")
        return fallback_servers

# Global variable to cache servers
_servers_cache = None
_servers_cache_time = 0

async def get_servers():
    """Get servers with caching"""
    global _servers_cache, _servers_cache_time
    import time
    
    current_time = time.time()
    # Cache for 1 minute (shorter than tools cache since config might change)
    if _servers_cache is None or (current_time - _servers_cache_time) > 60:
        logger.info("Refreshing servers cache from config.json")
        _servers_cache = await load_servers_from_config()
        _servers_cache_time = current_time
    
    return _servers_cache

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
                                    
                                    # Create unique tool name
                                    unique_tool_name = f"{server}_{tool_name}"
                                    
                                    tool_info = {
                                        "name": unique_tool_name,
                                        "description": f"[{server.upper()}] {operation.get('description', operation.get('summary', ''))}",
                                        "inputSchema": actual_schema,
                                        "_server": server,
                                        "_original_name": tool_name,
                                        "_endpoint_path": endpoint_path,
                                        "_original_schema": actual_schema  # Store original for parameter mapping
                                    }
                                    
                                    all_tools.append(tool_info)
                                    logger.info(f"    Added tool: {unique_tool_name} -> /{endpoint_path}")
                    else:
                        logger.error(f"Failed to fetch OpenAPI for {server}: HTTP {resp.status}")
            
            except Exception as e:
                logger.error(f"Failed to get tools from {server}: {e}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
    
    logger.info(f"=== Total tools loaded: {len(all_tools)} ===")
    return all_tools

# Cache the tools to avoid repeated API calls
_tools_cache = None
_tools_cache_time = 0

async def get_cached_tools():
    """Get tools with caching to avoid repeated OpenAPI calls"""
    global _tools_cache, _tools_cache_time
    import time
    
    current_time = time.time()
    # Cache for 5 minutes
    if _tools_cache is None or (current_time - _tools_cache_time) > 300:
        logger.info("Refreshing tools cache from OpenAPI specs")
        _tools_cache = await get_all_tools()
        _tools_cache_time = current_time
    
    return _tools_cache

def transform_arguments_with_schema(arguments: dict, schema: dict, tool_name: str) -> dict:
    """Transform arguments based on the actual OpenAPI schema"""
    if not schema or schema.get("type") != "object":
        return arguments
    
    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])
    
    logger.info(f"Schema properties for {tool_name}: {list(properties.keys())}")
    logger.info(f"Required fields: {required_fields}")
    
    transformed = {}
    
    # If we have a generic 'input' parameter, try to map it intelligently
    if "input" in arguments and len(arguments) == 1:
        input_value = arguments["input"]
        
        # Try to map to the first required field, or most likely field
        if required_fields:
            primary_field = required_fields[0]
            transformed[primary_field] = input_value
            logger.info(f"Mapped 'input' to required field '{primary_field}'")
        elif "path" in properties:
            transformed["path"] = input_value
            logger.info(f"Mapped 'input' to 'path' field")
        elif "file" in properties:
            transformed["file"] = input_value
            logger.info(f"Mapped 'input' to 'file' field")
        elif "code" in properties:
            transformed["code"] = input_value
            logger.info(f"Mapped 'input' to 'code' field")
        elif "query" in properties:
            transformed["query"] = input_value
            logger.info(f"Mapped 'input' to 'query' field")
        else:
            # Use the first available property
            if properties:
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
            if "default" in prop_schema:
                transformed[field] = prop_schema["default"]
                logger.info(f"Added default value for '{field}': {prop_schema['default']}")
            elif prop_schema.get("type") == "string":
                transformed[field] = ""
                logger.info(f"Added empty string for required field '{field}'")
            elif prop_schema.get("type") == "array":
                transformed[field] = []
                logger.info(f"Added empty array for required field '{field}'")
    
    return transformed

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
            
            logger.info(f"Calling tool via HTTP Streamable: {tool_name} with args: {arguments}")
            
            # Find the tool info from our cached tools
            all_tools = await get_cached_tools()
            tool_info = next((t for t in all_tools if t["name"] == tool_name), None)
            
            if not tool_info:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": f"Tool not found: {tool_name}"}
                })
            
            server_name = tool_info["_server"]
            endpoint_path = tool_info["_endpoint_path"]
            original_schema = tool_info["_original_schema"]
            
            # Transform arguments using the actual schema
            transformed_args = transform_arguments_with_schema(arguments, original_schema, tool_name)
            
            logger.info(f"Original args: {arguments}")
            logger.info(f"Transformed args: {transformed_args}")
            
            # Construct the endpoint URL using the actual path from OpenAPI
            endpoint_url = f"{MCPO_BASE}/{server_name}/{endpoint_path}"
            logger.info(f"Calling MCPO endpoint: {endpoint_url}")
            
            # Execute tool via MCPO
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(endpoint_url, json=transformed_args, timeout=300) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            
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
                            error_text = await resp.text()
                            logger.error(f"MCPO error {resp.status}: {error_text}")
                            return JSONResponse({
                                "jsonrpc": "2.0",
                                "id": msg_id,
                                "error": {"code": -32603, "message": f"Tool execution failed: {error_text}"}
                            })
                except Exception as e:
                    logger.error(f"Tool execution error: {e}")
                    return JSONResponse({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": str(e)}
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
            tools = await get_all_tools()
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
            # Same tool execution logic as HTTP Streamable
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            
            logger.info(f"Calling legacy tool: {tool_name} with args: {arguments}")
            
            # Parse server and tool name
            if "_" in tool_name:
                server_name = tool_name.split("_")[0]
                original_tool_name = "_".join(tool_name.split("_")[1:])
            else:
                all_tools = await get_all_tools()
                matching_tool = next((t for t in all_tools if t["name"] == tool_name), None)
                if matching_tool:
                    server_name = matching_tool["_server"]
                    original_tool_name = matching_tool["_original_name"]
                else:
                    return JSONResponse({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32602, "message": f"Tool not found: {tool_name}"}
                    })
            
            if server_name not in servers:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": f"Unknown server: {server_name}"}
                })
            
            # Use the same transformation logic as HTTP Streamable
            transformed_args = {}
            endpoint_name = original_tool_name
            if endpoint_name.endswith("_post"):
                endpoint_name = endpoint_name[:-5]
            
            # Apply the same parameter transformations as HTTP Streamable
            if server_name == "filesystem":
                if endpoint_name == "list_allowed_directories":
                    transformed_args = {}
                elif endpoint_name in ["list_directory", "list_directory_with_sizes", "directory_tree", 
                                     "read_text_file", "read_file", "read_media_file", "get_file_info", "create_directory"]:
                    if "input" in arguments:
                        transformed_args["path"] = arguments["input"]
                    elif "path" in arguments:
                        transformed_args["path"] = arguments["path"]
                    else:
                        transformed_args["path"] = ""
                # ... (same logic as HTTP Streamable)
            elif server_name == "python_executor":
                if endpoint_name == "run_script":
                    if "input" in arguments:
                        transformed_args["file"] = arguments["input"]
                    else:
                        transformed_args.update(arguments)
                elif endpoint_name == "python_executor":
                    if "input" in arguments:
                        transformed_args["code"] = arguments["input"]
                    else:
                        transformed_args.update(arguments)
            else:
                transformed_args.update(arguments)
            
            # Execute tool via MCPO using clean endpoint name
            async with aiohttp.ClientSession() as session:
                endpoint_url = f"{MCPO_BASE}/{server_name}/{endpoint_name}"
                
                try:
                    async with session.post(endpoint_url, json=transformed_args, timeout=300) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            
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
                            error_text = await resp.text()
                            logger.error(f"MCPO error {resp.status}: {error_text}")
                            return JSONResponse({
                                "jsonrpc": "2.0",
                                "id": msg_id,
                                "error": {"code": -32603, "message": f"Tool execution failed: {error_text}"}
                            })
                except Exception as e:
                    logger.error(f"Tool execution error: {e}")
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
