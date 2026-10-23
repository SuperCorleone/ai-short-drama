#!/usr/bin/env node
/**
 * Godot MCP Server
 *
 * This MCP server provides tools for interacting with the Godot game engine.
 * It enables AI assistants to launch the Godot editor, run Godot projects,
 * capture debug output, and control project execution.
 */

import { fileURLToPath } from 'url';
import { join, dirname, basename, normalize } from 'path';
import { existsSync, readdirSync, mkdirSync, rmSync } from 'fs';
import { spawn, execFile } from 'child_process';
import { promisify } from 'util';

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ErrorCode,
  ListToolsRequestSchema,
  McpError,
} from '@modelcontextprotocol/sdk/types.js';

// Check if debug mode is enabled
const DEBUG_MODE: boolean = process.env.DEBUG === 'true';
const GODOT_DEBUG_MODE: boolean = true; // Always use GODOT DEBUG MODE

const execFileAsync = promisify(execFile);

// Derive __filename and __dirname in ESM
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * Interface representing a running Godot process
 */
interface GodotProcess {
  process: any;
  output: string[];
  errors: string[];
}

/**
 * Interface for server configuration
 */
interface GodotServerConfig {
  godotPath?: string;
  debugMode?: boolean;
  godotDebugMode?: boolean;
  strictPathValidation?: boolean; // New option to control path validation behavior
}

/**
 * Interface for operation parameters
 */
interface OperationParams {
  [key: string]: any;
}

/**
 * Main server class for the Godot MCP server
 */
class GodotServer {
  private server: Server;
  private activeProcess: GodotProcess | null = null;
  private godotPath: string | null = null;
  private operationsScriptPath: string;
  private validatedPaths: Map<string, boolean> = new Map();
  private strictPathValidation: boolean = false;

  /**
   * Parameter name mappings between snake_case and camelCase
   * This allows the server to accept both formats
   */
  private parameterMappings: Record<string, string> = {
    'project_path': 'projectPath',
    'scene_path': 'scenePath',
    'root_node_type': 'rootNodeType',
    'parent_node_path': 'parentNodePath',
    'node_type': 'nodeType',
    'node_name': 'nodeName',
    'texture_path': 'texturePath',
    'node_path': 'nodePath',
    'output_path': 'outputPath',
    'mesh_item_names': 'meshItemNames',
    'new_path': 'newPath',
    'file_path': 'filePath',
    'directory': 'directory',
    'recursive': 'recursive',
    'scene': 'scene',
    'light_type': 'lightType',
    'source_node': 'sourceNode',
    'target_node': 'targetNode',
    'player_node_path': 'playerNodePath',
    'animation_name': 'animationName',
    'track_type': 'trackType',
    'target_node_path': 'targetNodePath',
    'track_index': 'trackIndex',
    'camera_node_path': 'cameraNodePath',
    'duration_sec': 'durationSec',
    'look_at': 'lookAt',
    'rotation_degrees': 'rotationDegrees',
    'buffer_type': 'bufferType',
  };

  /**
   * Reverse mapping from camelCase to snake_case
   * Generated from parameterMappings for quick lookups
   */
  private reverseParameterMappings: Record<string, string> = {};

  constructor(config?: GodotServerConfig) {
    // Initialize reverse parameter mappings
    for (const [snakeCase, camelCase] of Object.entries(this.parameterMappings)) {
      this.reverseParameterMappings[camelCase] = snakeCase;
    }
    // Apply configuration if provided
    let debugMode = DEBUG_MODE;
    let godotDebugMode = GODOT_DEBUG_MODE;

    if (config) {
      if (config.debugMode !== undefined) {
        debugMode = config.debugMode;
      }
      if (config.godotDebugMode !== undefined) {
        godotDebugMode = config.godotDebugMode;
      }
      if (config.strictPathValidation !== undefined) {
        this.strictPathValidation = config.strictPathValidation;
      }

      // Store and validate custom Godot path if provided
      if (config.godotPath) {
        const normalizedPath = normalize(config.godotPath);
        this.godotPath = normalizedPath;
        this.logDebug(`Custom Godot path provided: ${this.godotPath}`);

        // Validate immediately with sync check
        if (!this.isValidGodotPathSync(this.godotPath)) {
          console.warn(`[SERVER] Invalid custom Godot path provided: ${this.godotPath}`);
          this.godotPath = null; // Reset to trigger auto-detection later
        }
      }
    }

    // Set the path to the operations script
    this.operationsScriptPath = join(__dirname, 'scripts', 'godot_operations.gd');
    if (debugMode) console.error(`[DEBUG] Operations script path: ${this.operationsScriptPath}`);

    // Initialize the MCP server
    this.server = new Server(
      {
        name: 'godot-mcp',
        version: '0.1.0',
      },
      {
        capabilities: {
          tools: {},
        },
      }
    );

    // Set up tool handlers
    this.setupToolHandlers();

    // Error handling
    this.server.onerror = (error) => console.error('[MCP Error]', error);

    // Cleanup on exit
    process.on('SIGINT', async () => {
      await this.cleanup();
      process.exit(0);
    });
  }

  /**
   * Log debug messages if debug mode is enabled
   * Using stderr instead of stdout to avoid interfering with JSON-RPC communication
   */
  private logDebug(message: string): void {
    if (DEBUG_MODE) {
      console.error(`[DEBUG] ${message}`);
    }
  }

  /**
   * Create a standardized error response with possible solutions
   */
  private createErrorResponse(message: string, possibleSolutions: string[] = []): any {
    // Log the error
    console.error(`[SERVER] Error response: ${message}`);
    if (possibleSolutions.length > 0) {
      console.error(`[SERVER] Possible solutions: ${possibleSolutions.join(', ')}`);
    }

    const response: any = {
      content: [
        {
          type: 'text',
          text: message,
        },
      ],
      isError: true,
    };

    if (possibleSolutions.length > 0) {
      response.content.push({
        type: 'text',
        text: 'Possible solutions:\n- ' + possibleSolutions.join('\n- '),
      });
    }

    return response;
  }

  /**
   * Validate a path to prevent path traversal attacks
   */
  private validatePath(path: string): boolean {
    // Basic validation to prevent path traversal
    if (!path || path.includes('..')) {
      return false;
    }

    // Add more validation as needed
    return true;
  }

  /**
   * Validate a Godot class name to prevent arbitrary script instantiation.
   * Class names must be simple identifiers (e.g. "Node2D", "CharacterBody3D").
   * Rejects anything that looks like a path (res://, absolute paths, dots, slashes, colons).
   */
  private validateClassName(name: string): boolean {
    if (!name) return false;
    return /^[A-Za-z_][A-Za-z0-9_]*$/.test(name);
  }

  /**
   * Synchronous validation for constructor use
   * This is a quick check that only verifies file existence, not executable validity
   * Full validation will be performed later in detectGodotPath
   * @param path Path to check
   * @returns True if the path exists or is 'godot' (which might be in PATH)
   */
  private isValidGodotPathSync(path: string): boolean {
    try {
      this.logDebug(`Quick-validating Godot path: ${path}`);
      return path === 'godot' || existsSync(path);
    } catch (error) {
      this.logDebug(`Invalid Godot path: ${path}, error: ${error}`);
      return false;
    }
  }

  /**
   * Validate if a Godot path is valid and executable
   */
  private async isValidGodotPath(path: string): Promise<boolean> {
    // Check cache first
    if (this.validatedPaths.has(path)) {
      return this.validatedPaths.get(path)!;
    }

    try {
      this.logDebug(`Validating Godot path: ${path}`);

      // Check if the file exists (skip for 'godot' which might be in PATH)
      if (path !== 'godot' && !existsSync(path)) {
        this.logDebug(`Path does not exist: ${path}`);
        this.validatedPaths.set(path, false);
        return false;
      }

      // Try to execute Godot with --version flag
      // Using execFileAsync with argument array to prevent command injection
      await execFileAsync(path, ['--version']);

      this.logDebug(`Valid Godot path: ${path}`);
      this.validatedPaths.set(path, true);
      return true;
    } catch (error) {
      this.logDebug(`Invalid Godot path: ${path}, error: ${error}`);
      this.validatedPaths.set(path, false);
      return false;
    }
  }

  /**
   * Detect the Godot executable path based on the operating system
   */
  private async detectGodotPath() {
    // If godotPath is already set and valid, use it
    if (this.godotPath && await this.isValidGodotPath(this.godotPath)) {
      this.logDebug(`Using existing Godot path: ${this.godotPath}`);
      return;
    }

    // Check environment variable next
    if (process.env.GODOT_PATH) {
      const normalizedPath = normalize(process.env.GODOT_PATH);
      this.logDebug(`Checking GODOT_PATH environment variable: ${normalizedPath}`);
      if (await this.isValidGodotPath(normalizedPath)) {
        this.godotPath = normalizedPath;
        this.logDebug(`Using Godot path from environment: ${this.godotPath}`);
        return;
      } else {
        this.logDebug(`GODOT_PATH environment variable is invalid`);
      }
    }

    // Auto-detect based on platform
    const osPlatform = process.platform;
    this.logDebug(`Auto-detecting Godot path for platform: ${osPlatform}`);

    const possiblePaths: string[] = [
      'godot', // Check if 'godot' is in PATH first
    ];

    // Add platform-specific paths
    if (osPlatform === 'darwin') {
      possiblePaths.push(
        '/Applications/Godot.app/Contents/MacOS/Godot',
        '/Applications/Godot_4.app/Contents/MacOS/Godot',
        `${process.env.HOME}/Applications/Godot.app/Contents/MacOS/Godot`,
        `${process.env.HOME}/Applications/Godot_4.app/Contents/MacOS/Godot`,
        `${process.env.HOME}/Library/Application Support/Steam/steamapps/common/Godot Engine/Godot.app/Contents/MacOS/Godot`
      );
    } else if (osPlatform === 'win32') {
      possiblePaths.push(
        'C:\\Program Files\\Godot\\Godot.exe',
        'C:\\Program Files (x86)\\Godot\\Godot.exe',
        'C:\\Program Files\\Godot_4\\Godot.exe',
        'C:\\Program Files (x86)\\Godot_4\\Godot.exe',
        `${process.env.USERPROFILE}\\Godot\\Godot.exe`
      );
    } else if (osPlatform === 'linux') {
      possiblePaths.push(
        '/usr/bin/godot',
        '/usr/local/bin/godot',
        '/snap/bin/godot',
        `${process.env.HOME}/.local/bin/godot`
      );
    }

    // Try each possible path
    for (const path of possiblePaths) {
      const normalizedPath = normalize(path);
      if (await this.isValidGodotPath(normalizedPath)) {
        this.godotPath = normalizedPath;
        this.logDebug(`Found Godot at: ${normalizedPath}`);
        return;
      }
    }

    // If we get here, we couldn't find Godot
    this.logDebug(`Warning: Could not find Godot in common locations for ${osPlatform}`);
    console.error(`[SERVER] Could not find Godot in common locations for ${osPlatform}`);
    console.error(`[SERVER] Set GODOT_PATH=/path/to/godot environment variable or pass { godotPath: '/path/to/godot' } in the config to specify the correct path.`);

    if (this.strictPathValidation) {
      // In strict mode, throw an error
      throw new Error(`Could not find a valid Godot executable. Set GODOT_PATH or provide a valid path in config.`);
    } else {
      // Fallback to a default path in non-strict mode; this may not be valid and requires user configuration for reliability
      if (osPlatform === 'win32') {
        this.godotPath = normalize('C:\\Program Files\\Godot\\Godot.exe');
      } else if (osPlatform === 'darwin') {
        this.godotPath = normalize('/Applications/Godot.app/Contents/MacOS/Godot');
      } else {
        this.godotPath = normalize('/usr/bin/godot');
      }

      this.logDebug(`Using default path: ${this.godotPath}, but this may not work.`);
      console.error(`[SERVER] Using default path: ${this.godotPath}, but this may not work.`);
      console.error(`[SERVER] This fallback behavior will be removed in a future version. Set strictPathValidation: true to opt-in to the new behavior.`);
    }
  }

  /**
   * Set a custom Godot path
   * @param customPath Path to the Godot executable
   * @returns True if the path is valid and was set, false otherwise
   */
  public async setGodotPath(customPath: string): Promise<boolean> {
    if (!customPath) {
      return false;
    }

    // Normalize the path to ensure consistent format across platforms
    // (e.g., backslashes to forward slashes on Windows, resolving relative paths)
    const normalizedPath = normalize(customPath);
    if (await this.isValidGodotPath(normalizedPath)) {
      this.godotPath = normalizedPath;
      this.logDebug(`Godot path set to: ${normalizedPath}`);
      return true;
    }

    this.logDebug(`Failed to set invalid Godot path: ${normalizedPath}`);
    return false;
  }

  /**
   * Clean up resources when shutting down
   */
  private async cleanup() {
    this.logDebug('Cleaning up resources');
    if (this.activeProcess) {
      this.logDebug('Killing active Godot process');
      this.activeProcess.process.kill();
      this.activeProcess = null;
    }
    await this.server.close();
  }

  /**
   * Check if the Godot version is 4.4 or later
   * @param version The Godot version string
   * @returns True if the version is 4.4 or later
   */
  private isGodot44OrLater(version: string): boolean {
    const match = version.match(/^(\d+)\.(\d+)/);
    if (match) {
      const major = parseInt(match[1], 10);
      const minor = parseInt(match[2], 10);
      return major > 4 || (major === 4 && minor >= 4);
    }
    return false;
  }

  /**
   * Normalize parameters to camelCase format
   * @param params Object with either snake_case or camelCase keys
   * @returns Object with all keys in camelCase format
   */
  private normalizeParameters(params: OperationParams): OperationParams {
    if (!params || typeof params !== 'object') {
      return params;
    }
    
    const result: OperationParams = {};
    
    for (const key in params) {
      if (Object.prototype.hasOwnProperty.call(params, key)) {
        let normalizedKey = key;
        
        // If the key is in snake_case, convert it to camelCase using our mapping
        if (key.includes('_') && this.parameterMappings[key]) {
          normalizedKey = this.parameterMappings[key];
        }
        
        // Handle nested objects recursively
        if (typeof params[key] === 'object' && params[key] !== null && !Array.isArray(params[key])) {
          result[normalizedKey] = this.normalizeParameters(params[key] as OperationParams);
        } else {
          result[normalizedKey] = params[key];
        }
      }
    }
    
    return result;
  }

  /**
   * Convert camelCase keys to snake_case
   * @param params Object with camelCase keys
   * @returns Object with snake_case keys
   */
  private convertCamelToSnakeCase(params: OperationParams): OperationParams {
    const result: OperationParams = {};
    
    for (const key in params) {
      if (Object.prototype.hasOwnProperty.call(params, key)) {
        // Convert camelCase to snake_case
        const snakeKey = this.reverseParameterMappings[key] || key.replace(/[A-Z]/g, letter => `_${letter.toLowerCase()}`);
        
        // Handle nested objects recursively
        if (typeof params[key] === 'object' && params[key] !== null && !Array.isArray(params[key])) {
          result[snakeKey] = this.convertCamelToSnakeCase(params[key] as OperationParams);
        } else {
          result[snakeKey] = params[key];
        }
      }
    }
    
    return result;
  }

  /**
   * Execute a Godot operation using the operations script
   * @param operation The operation to execute
   * @param params The parameters for the operation
   * @param projectPath The path to the Godot project
   * @param headless Whether to run Godot with --headless (default: true).
   *   Rendering operations must pass false so a real window/rendering driver is used.
   * @returns The stdout and stderr from the operation
   */
  private async executeOperation(
    operation: string,
    params: OperationParams,
    projectPath: string,
    headless: boolean = true
  ): Promise<{ stdout: string; stderr: string }> {
    this.logDebug(`Executing operation: ${operation} in project: ${projectPath}`);
    this.logDebug(`Original operation params: ${JSON.stringify(params)}`);

    // Convert camelCase parameters to snake_case for Godot script
    const snakeCaseParams = this.convertCamelToSnakeCase(params);
    this.logDebug(`Converted snake_case params: ${JSON.stringify(snakeCaseParams)}`);


    // Ensure godotPath is set
    if (!this.godotPath) {
      await this.detectGodotPath();
      if (!this.godotPath) {
        throw new Error('Could not find a valid Godot executable path');
      }
    }

    try {
      // Serialize the snake_case parameters to a valid JSON string
      const paramsJson = JSON.stringify(snakeCaseParams);

      // Build argument array for execFile to prevent command injection
      // Using execFile with argument arrays avoids shell interpretation entirely
      const args = [
        '--path',
        projectPath,  // Safe: passed as argument, not interpolated into shell command
        '--script',
        this.operationsScriptPath,
        operation,
        paramsJson,  // Safe: passed as argument, not interpreted by shell
      ];

      // --headless uses the dummy rasterizer (no GPU rendering), so rendering
      // operations run windowed instead; the script quits itself after capture.
      if (headless) {
        args.unshift('--headless');
      }

      
      if (GODOT_DEBUG_MODE) {
        args.push('--debug-godot');
      }

      this.logDebug(`Executing: ${this.godotPath} ${args.join(' ')}`);

      const { stdout, stderr } = await execFileAsync(this.godotPath!, args);

      return { stdout: stdout ?? '', stderr: stderr ?? '' };
    } catch (error: unknown) {
      // If execFileAsync throws, it still contains stdout/stderr
      if (error instanceof Error && 'stdout' in error && 'stderr' in error) {
        const execError = error as Error & { stdout: string; stderr: string };
        return {
          stdout: execError.stdout ?? '',
          stderr: execError.stderr ?? '',
        };
      }

      throw error;
    }
  }

  /**
   * Get the structure of a Godot project
   * @param projectPath Path to the Godot project
   * @returns Object representing the project structure
   */
  private async getProjectStructure(projectPath: string): Promise<any> {
    try {
      // Get top-level directories in the project
      const entries = readdirSync(projectPath, { withFileTypes: true });

      const structure: any = {
        scenes: [],
        scripts: [],
        assets: [],
        other: [],
      };

      for (const entry of entries) {
        if (entry.isDirectory()) {
          const dirName = entry.name.toLowerCase();

          // Skip hidden directories
          if (dirName.startsWith('.')) {
            continue;
          }

          // Count files in common directories
          if (dirName === 'scenes' || dirName.includes('scene')) {
            structure.scenes.push(entry.name);
          } else if (dirName === 'scripts' || dirName.includes('script')) {
            structure.scripts.push(entry.name);
          } else if (
            dirName === 'assets' ||
            dirName === 'textures' ||
            dirName === 'models' ||
            dirName === 'sounds' ||
            dirName === 'music'
          ) {
            structure.assets.push(entry.name);
          } else {
            structure.other.push(entry.name);
          }
        }
      }

      return structure;
    } catch (error) {
      this.logDebug(`Error getting project structure: ${error}`);
      return { error: 'Failed to get project structure' };
    }
  }

  /**
   * Find Godot projects in a directory
   * @param directory Directory to search
   * @param recursive Whether to search recursively
   * @returns Array of Godot projects
   */
  private findGodotProjects(directory: string, recursive: boolean): Array<{ path: string; name: string }> {
    const projects: Array<{ path: string; name: string }> = [];

    try {
      // Check if the directory itself is a Godot project
      const projectFile = join(directory, 'project.godot');
      if (existsSync(projectFile)) {
        projects.push({
          path: directory,
          name: basename(directory),
        });
      }

      // If not recursive, only check immediate subdirectories
      if (!recursive) {
        const entries = readdirSync(directory, { withFileTypes: true });
        for (const entry of entries) {
          if (entry.isDirectory()) {
            const subdir = join(directory, entry.name);
            const projectFile = join(subdir, 'project.godot');
            if (existsSync(projectFile)) {
              projects.push({
                path: subdir,
                name: entry.name,
              });
            }
          }
        }
      } else {
        // Recursive search
        const entries = readdirSync(directory, { withFileTypes: true });
        for (const entry of entries) {
          if (entry.isDirectory()) {
            const subdir = join(directory, entry.name);
            // Skip hidden directories
            if (entry.name.startsWith('.')) {
              continue;
            }
            // Check if this directory is a Godot project
            const projectFile = join(subdir, 'project.godot');
            if (existsSync(projectFile)) {
              projects.push({
                path: subdir,
                name: entry.name,
              });
            } else {
              // Recursively search this directory
              const subProjects = this.findGodotProjects(subdir, true);
              projects.push(...subProjects);
            }
          }
        }
      }
    } catch (error) {
      this.logDebug(`Error searching directory ${directory}: ${error}`);
    }

    return projects;
  }

  /**
   * Set up the tool handlers for the MCP server
   */
  private setupToolHandlers() {
    // Define available tools
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: [
        {
          name: 'launch_editor',
          description: 'Launch Godot editor for a specific project',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
            },
            required: ['projectPath'],
          },
        },
        {
          name: 'run_project',
          description: 'Run the Godot project and capture output',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scene: {
                type: 'string',
                description: 'Optional: Specific scene to run',
              },
            },
            required: ['projectPath'],
          },
        },
        {
          name: 'get_debug_output',
          description: 'Get the current debug output and errors',
          inputSchema: {
            type: 'object',
            properties: {},
            required: [],
          },
        },
        {
          name: 'stop_project',
          description: 'Stop the currently running Godot project',
          inputSchema: {
            type: 'object',
            properties: {},
            required: [],
          },
        },
        {
          name: 'get_godot_version',
          description: 'Get the installed Godot version',
          inputSchema: {
            type: 'object',
            properties: {},
            required: [],
          },
        },
        {
          name: 'list_projects',
          description: 'List Godot projects in a directory',
          inputSchema: {
            type: 'object',
            properties: {
              directory: {
                type: 'string',
                description: 'Directory to search for Godot projects',
              },
              recursive: {
                type: 'boolean',
                description: 'Whether to search recursively (default: false)',
              },
            },
            required: ['directory'],
          },
        },
        {
          name: 'get_project_info',
          description: 'Retrieve metadata about a Godot project',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
            },
            required: ['projectPath'],
          },
        },
        {
          name: 'create_scene',
          description: 'Create a new Godot scene file',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path where the scene file will be saved (relative to project)',
              },
              rootNodeType: {
                type: 'string',
                description: 'Type of the root node (e.g., Node2D, Node3D)',
              },
            },
            required: ['projectPath', 'scenePath'],
          },
        },
        {
          name: 'add_node',
          description: 'Add a node to an existing scene',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              parentNodePath: {
                type: 'string',
                description: 'Path to the parent node (e.g., "root" or "root/Player")',
              },
              nodeType: {
                type: 'string',
                description: 'Type of node to add (e.g., Sprite2D, CollisionShape2D)',
              },
              nodeName: {
                type: 'string',
                description: 'Name for the new node',
              },
              properties: {
                type: 'object',
                description: 'Optional properties to set on the node',
              },
            },
            required: ['projectPath', 'scenePath', 'nodeType', 'nodeName'],
          },
        },
        {
          name: 'load_sprite',
          description: 'Load a sprite into a Sprite2D node',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              nodePath: {
                type: 'string',
                description: 'Path to the Sprite2D node (e.g., "root/Player/Sprite2D")',
              },
              texturePath: {
                type: 'string',
                description: 'Path to the texture file (relative to project)',
              },
            },
            required: ['projectPath', 'scenePath', 'nodePath', 'texturePath'],
          },
        },
        {
          name: 'export_mesh_library',
          description: 'Export a scene as a MeshLibrary resource',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (.tscn) to export',
              },
              outputPath: {
                type: 'string',
                description: 'Path where the mesh library (.res) will be saved',
              },
              meshItemNames: {
                type: 'array',
                items: {
                  type: 'string',
                },
                description: 'Optional: Names of specific mesh items to include (defaults to all)',
              },
            },
            required: ['projectPath', 'scenePath', 'outputPath'],
          },
        },
        {
          name: 'save_scene',
          description: 'Save changes to a scene file',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              newPath: {
                type: 'string',
                description: 'Optional: New path to save the scene to (for creating variants)',
              },
            },
            required: ['projectPath', 'scenePath'],
          },
        },
        {
          name: 'get_uid',
          description: 'Get the UID for a specific file in a Godot project (for Godot 4.4+)',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              filePath: {
                type: 'string',
                description: 'Path to the file (relative to project) for which to get the UID',
              },
            },
            required: ['projectPath', 'filePath'],
          },
        },
        {
          name: 'update_project_uids',
          description: 'Update UID references in a Godot project by resaving resources (for Godot 4.4+)',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
            },
            required: ['projectPath'],
          },
        },
        {
          name: 'set_lighting',
          description: 'Create or update a light node (DirectionalLight3D, OmniLight3D, SpotLight3D) in a scene',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              lightType: {
                type: 'string',
                description: 'Type of light node (DirectionalLight3D, OmniLight3D, or SpotLight3D)',
                enum: ['DirectionalLight3D', 'OmniLight3D', 'SpotLight3D'],
              },
              nodeName: {
                type: 'string',
                description: 'Name of the light node to create or update',
              },
              transform: {
                type: 'object',
                description: 'Optional transform with position {x,y,z} and rotation_degrees {x,y,z}',
              },
              properties: {
                type: 'object',
                description: 'Optional light properties: color ([r,g,b] or [r,g,b,a]), energy (number), shadow_enabled (boolean)',
              },
            },
            required: ['projectPath', 'scenePath', 'lightType', 'nodeName'],
          },
        },
        {
          name: 'place_camera',
          description: 'Create or update a Camera3D in a scene, position it, aim it at a target, and set it current',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              nodeName: {
                type: 'string',
                description: 'Name of the camera node to create or update',
              },
              position: {
                type: 'object',
                description: 'Camera position {x,y,z}',
              },
              lookAt: {
                type: 'object',
                description: 'Target point {x,y,z} the camera should aim at',
              },
              fov: {
                type: 'number',
                description: 'Optional field of view in degrees (default: 75)',
              },
            },
            required: ['projectPath', 'scenePath', 'nodeName', 'position', 'lookAt'],
          },
        },
        {
          name: 'build_graybox_set',
          description: 'Build a complete graybox previs set in one call: creates a fresh scene from a JSON node list (box/capsule/sphere/plane/label/light) and saves it. Batch alternative to many add_node calls (one Godot process per set instead of one per node).',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path where the scene file will be saved (relative to project); overwritten if it exists',
              },
              nodes: {
                type: 'array',
                description: 'Nodes to build: [{type: box|capsule|sphere|plane|label|light, name, position: {x,y,z}, size: {x,y,z} (box/plane extents, capsule [x=radius, y=height], sphere [x=radius]), color?: [r,g,b], text? (label), lightType? (light), rotation?: {x,y,z} degrees (light), energy? (light)}]',
                items: {
                  type: 'object',
                },
              },
              camera: {
                type: 'object',
                description: 'Optional default camera saved into the scene: {position: {x,y,z}, lookAt: {x,y,z}, fov?: number}',
              },
            },
            required: ['projectPath', 'scenePath', 'nodes'],
          },
        },
        {
          name: 'attach_look_at_constraint',
          description: 'Make a Node3D continuously face another node at runtime (attaches a small look_at script; LookAtModifier3D requires a Skeleton3D so it is not used for plain nodes)',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              sourceNode: {
                type: 'string',
                description: 'Path to the node that should face the target (e.g., "root/Box")',
              },
              targetNode: {
                type: 'string',
                description: 'Path to the node to look at (e.g., "root/Target")',
              },
            },
            required: ['projectPath', 'scenePath', 'sourceNode', 'targetNode'],
          },
        },
        {
          name: 'create_animation',
          description: 'Create a new animation in an AnimationPlayer node (stored in the default AnimationLibrary)',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              playerNodePath: {
                type: 'string',
                description: 'Path to the AnimationPlayer node (e.g., "root/AnimationPlayer")',
              },
              animationName: {
                type: 'string',
                description: 'Name of the animation to create',
              },
              length: {
                type: 'number',
                description: 'Optional animation length in seconds (default: 1.0)',
              },
            },
            required: ['projectPath', 'scenePath', 'playerNodePath', 'animationName'],
          },
        },
        {
          name: 'add_track',
          description: 'Add a track (value, position_3d, rotation_3d, or scale_3d) to an animation',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              playerNodePath: {
                type: 'string',
                description: 'Path to the AnimationPlayer node (e.g., "root/AnimationPlayer")',
              },
              animationName: {
                type: 'string',
                description: 'Name of the animation',
              },
              trackType: {
                type: 'string',
                description: 'Type of track to add',
                enum: ['value', 'position_3d', 'rotation_3d', 'scale_3d'],
              },
              targetNodePath: {
                type: 'string',
                description: 'Path to the node the track animates (e.g., "root/Box")',
              },
              property: {
                type: 'string',
                description: 'Property name (required for value tracks, e.g., "visible")',
              },
              keyframes: {
                type: 'array',
                description: 'Optional keyframes to fill the track in one call: [{time: number, value: any, transition?: number}]',
                items: {
                  type: 'object',
                  properties: {
                    time: {
                      type: 'number',
                      description: 'Key time in seconds',
                    },
                    value: {
                      description: 'Key value: number/boolean/string, [x,y] for Vector2, [x,y,z] for Vector3, [x,y,z,w] for Quaternion (rotation_3d tracks)',
                    },
                    transition: {
                      type: 'number',
                      description: 'Optional easing/transition for this key (default: 1.0)',
                    },
                  },
                  required: ['time', 'value'],
                },
              },
            },
            required: ['projectPath', 'scenePath', 'playerNodePath', 'animationName', 'trackType', 'targetNodePath'],
          },
        },
        {
          name: 'insert_keyframe',
          description: 'Insert a keyframe into an animation track. Upsert: if a key already exists at the same time it is updated in place.',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              playerNodePath: {
                type: 'string',
                description: 'Path to the AnimationPlayer node (e.g., "root/AnimationPlayer")',
              },
              animationName: {
                type: 'string',
                description: 'Name of the animation',
              },
              trackIndex: {
                type: 'number',
                description: 'Index of the track (returned by add_track)',
              },
              time: {
                type: 'number',
                description: 'Key time in seconds',
              },
              value: {
                description: 'Key value: number/boolean/string, [x,y] for Vector2, [x,y,z] for Vector3, [x,y,z,w] for Quaternion (rotation_3d tracks)',
              },
              transition: {
                type: 'number',
                description: 'Optional easing/transition for this key (default: 1.0)',
              },
            },
            required: ['projectPath', 'scenePath', 'playerNodePath', 'animationName', 'trackIndex', 'time', 'value'],
          },
        },
        {
          name: 'set_animation_length',
          description: 'Set the length (in seconds) of an existing animation',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              playerNodePath: {
                type: 'string',
                description: 'Path to the AnimationPlayer node (e.g., "root/AnimationPlayer")',
              },
              animationName: {
                type: 'string',
                description: 'Name of the animation',
              },
              length: {
                type: 'number',
                description: 'New animation length in seconds',
              },
            },
            required: ['projectPath', 'scenePath', 'playerNodePath', 'animationName', 'length'],
          },
        },
        {
          name: 'render_viewport',
          description: 'Render one frame of a scene from its current (or specified) camera to a PNG file. Runs Godot windowed (not headless) since headless has no GPU rendering.',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              outputPath: {
                type: 'string',
                description: 'Path where the PNG will be saved (relative to project)',
              },
              resolution: {
                type: 'object',
                description: 'Optional resolution {width, height} (default: 1280x720)',
              },
              cameraNodePath: {
                type: 'string',
                description: 'Optional path to the Camera3D to render from (defaults to the first camera in the scene)',
              },
              camera: {
                type: 'object',
                description: 'Optional inline camera: {position: {x,y,z}, lookAt: {x,y,z}, fov?: number}. When present, a throwaway camera is used for this render only and the scene file is not modified (takes precedence over cameraNodePath).',
              },
              hideLabels: {
                type: 'boolean',
                description: 'Hide all Label3D annotation nodes for this render. Set true when the frame will be used as model conditioning (e.g. as the source for a canny map) — otherwise the lettering becomes strong fake edges. Leave false for human-facing previs frames.',
              },
            },
            required: ['projectPath', 'scenePath', 'outputPath'],
          },
        },
        {
          name: 'render_shot_batch',
          description: 'Render MANY captures of one scene in a SINGLE Godot process. godot-mcp is spawn-per-op, so an 82-shot x 3-channel previs export costs 246 process launches with ~1.5s of pure engine startup each; this collapses that to one launch per scene. Per-capture semantics are identical to render_viewport / capture_aux_buffer. Label3D annotations are always hidden (batch output is model conditioning, not a human-facing frame).',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: { type: 'string', description: 'Path to the Godot project directory' },
              scenePath: { type: 'string', description: 'Scene file to render, relative to the project' },
              resolution: { type: 'object', description: 'Optional {width, height} applied to every capture (default 1280x720)' },
              shots: {
                type: 'array',
                description: 'Captures to produce: [{shot_id, camera:{position,lookAt,fov?}, outputs:{rgb?,depth?,normal?}}]. Every output path is relative to the project.',
                items: { type: 'object' },
              },
            },
            required: ['projectPath', 'scenePath', 'shots'],
          },
        },
        {
          name: 'capture_aux_buffer',
          description: 'Capture an auxiliary render buffer (currently only depth) of a scene from its camera to a PNG file. Depth is linearized and normalized to grayscale (near = dark, far = bright). Runs Godot windowed (not headless).',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              outputPath: {
                type: 'string',
                description: 'Path where the PNG will be saved (relative to project)',
              },
              bufferType: {
                type: 'string',
                description: 'Auxiliary buffer to capture. "depth": view-space depth auto-stretched to grayscale. "normal": view-space normals encoded as RGB (n*0.5+0.5), the convention ControlNet normal models expect.',
                enum: ['depth', 'normal'],
              },
              resolution: {
                type: 'object',
                description: 'Optional resolution {width, height} (default: 1280x720)',
              },
              cameraNodePath: {
                type: 'string',
                description: 'Optional path to the Camera3D to render from (defaults to the first camera in the scene)',
              },
              camera: {
                type: 'object',
                description: 'Optional inline camera {position:{x,y,z}, look_at:{x,y,z}, fov?}. Use the SAME spec as render_viewport so the aux buffer lines up pixel-for-pixel with the colour frame.',
              },
            },
            required: ['projectPath', 'scenePath', 'outputPath'],
          },
        },
        {
          name: 'write_movie',
          description: 'Record a scene to a video using Godot Movie Maker mode. Runs windowed (not headless). format=avi writes MJPEG/.avi directly; format=png writes a PNG frame sequence and encodes it to .mp4 with ffmpeg (libx264, yuv420p).',
          inputSchema: {
            type: 'object',
            properties: {
              projectPath: {
                type: 'string',
                description: 'Path to the Godot project directory',
              },
              scenePath: {
                type: 'string',
                description: 'Path to the scene file (relative to project)',
              },
              outputPath: {
                type: 'string',
                description: 'Path where the video will be saved (relative to project; .avi for format=avi, .mp4 for format=png)',
              },
              format: {
                type: 'string',
                description: 'Optional output format: "avi" (default, MJPEG) or "png" (PNG frames encoded to .mp4 via ffmpeg)',
                enum: ['avi', 'png'],
              },
              durationSec: {
                type: 'number',
                description: 'Optional recording duration in seconds (default: 5)',
              },
              fps: {
                type: 'number',
                description: 'Optional frames per second (default: 30)',
              },
            },
            required: ['projectPath', 'scenePath', 'outputPath'],
          },
        },
      ],
    }));

    // Handle tool calls
    this.server.setRequestHandler(CallToolRequestSchema, async (request) => {
      this.logDebug(`Handling tool request: ${request.params.name}`);
      switch (request.params.name) {
        case 'launch_editor':
          return await this.handleLaunchEditor(request.params.arguments);
        case 'run_project':
          return await this.handleRunProject(request.params.arguments);
        case 'get_debug_output':
          return await this.handleGetDebugOutput();
        case 'stop_project':
          return await this.handleStopProject();
        case 'get_godot_version':
          return await this.handleGetGodotVersion();
        case 'list_projects':
          return await this.handleListProjects(request.params.arguments);
        case 'get_project_info':
          return await this.handleGetProjectInfo(request.params.arguments);
        case 'create_scene':
          return await this.handleCreateScene(request.params.arguments);
        case 'add_node':
          return await this.handleAddNode(request.params.arguments);
        case 'load_sprite':
          return await this.handleLoadSprite(request.params.arguments);
        case 'export_mesh_library':
          return await this.handleExportMeshLibrary(request.params.arguments);
        case 'save_scene':
          return await this.handleSaveScene(request.params.arguments);
        case 'get_uid':
          return await this.handleGetUid(request.params.arguments);
        case 'update_project_uids':
          return await this.handleUpdateProjectUids(request.params.arguments);
        case 'set_lighting':
          return await this.handleSetLighting(request.params.arguments);
        case 'place_camera':
          return await this.handlePlaceCamera(request.params.arguments);
        case 'build_graybox_set':
          return await this.handleBuildGrayboxSet(request.params.arguments);
        case 'attach_look_at_constraint':
          return await this.handleAttachLookAtConstraint(request.params.arguments);
        case 'create_animation':
          return await this.handleCreateAnimation(request.params.arguments);
        case 'add_track':
          return await this.handleAddTrack(request.params.arguments);
        case 'insert_keyframe':
          return await this.handleInsertKeyframe(request.params.arguments);
        case 'set_animation_length':
          return await this.handleSetAnimationLength(request.params.arguments);
        case 'render_viewport':
          return await this.handleRenderViewport(request.params.arguments);
        case 'render_shot_batch':
          return await this.handleRenderShotBatch(request.params.arguments);
        case 'capture_aux_buffer':
          return await this.handleCaptureAuxBuffer(request.params.arguments);
        case 'write_movie':
          return await this.handleWriteMovie(request.params.arguments);
        default:
          throw new McpError(
            ErrorCode.MethodNotFound,
            `Unknown tool: ${request.params.name}`
          );
      }
    });
  }

  /**
   * Handle the launch_editor tool
   * @param args Tool arguments
   */
  private async handleLaunchEditor(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath) {
      return this.createErrorResponse(
        'Project path is required',
        ['Provide a valid path to a Godot project directory']
      );
    }

    if (!this.validatePath(args.projectPath)) {
      return this.createErrorResponse(
        'Invalid project path',
        ['Provide a valid path without ".." or other potentially unsafe characters']
      );
    }

    try {
      // Ensure godotPath is set
      if (!this.godotPath) {
        await this.detectGodotPath();
        if (!this.godotPath) {
          return this.createErrorResponse(
            'Could not find a valid Godot executable path',
            [
              'Ensure Godot is installed correctly',
              'Set GODOT_PATH environment variable to specify the correct path',
            ]
          );
        }
      }

      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      this.logDebug(`Launching Godot editor for project: ${args.projectPath}`);
      const process = spawn(this.godotPath, ['-e', '--path', args.projectPath], {
        stdio: 'pipe',
      });

      process.on('error', (err: Error) => {
        console.error('Failed to start Godot editor:', err);
      });

      return {
        content: [
          {
            type: 'text',
            text: `Godot editor launched successfully for project at ${args.projectPath}.`,
          },
        ],
      };
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      return this.createErrorResponse(
        `Failed to launch Godot editor: ${errorMessage}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the run_project tool
   * @param args Tool arguments
   */
  private async handleRunProject(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath) {
      return this.createErrorResponse(
        'Project path is required',
        ['Provide a valid path to a Godot project directory']
      );
    }

    if (!this.validatePath(args.projectPath)) {
      return this.createErrorResponse(
        'Invalid project path',
        ['Provide a valid path without ".." or other potentially unsafe characters']
      );
    }

    try {
      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      // Kill any existing process
      if (this.activeProcess) {
        this.logDebug('Killing existing Godot process before starting a new one');
        this.activeProcess.process.kill();
      }

      const cmdArgs = ['-d', '--path', args.projectPath];
      if (args.scene && this.validatePath(args.scene)) {
        this.logDebug(`Adding scene parameter: ${args.scene}`);
        cmdArgs.push(args.scene);
      }

      this.logDebug(`Running Godot project: ${args.projectPath}`);
      const process = spawn(this.godotPath!, cmdArgs, { stdio: 'pipe' });
      const output: string[] = [];
      const errors: string[] = [];

      process.stdout?.on('data', (data: Buffer) => {
        const lines = data.toString().split('\n');
        output.push(...lines);
        lines.forEach((line: string) => {
          if (line.trim()) this.logDebug(`[Godot stdout] ${line}`);
        });
      });

      process.stderr?.on('data', (data: Buffer) => {
        const lines = data.toString().split('\n');
        errors.push(...lines);
        lines.forEach((line: string) => {
          if (line.trim()) this.logDebug(`[Godot stderr] ${line}`);
        });
      });

      process.on('exit', (code: number | null) => {
        this.logDebug(`Godot process exited with code ${code}`);
        if (this.activeProcess && this.activeProcess.process === process) {
          this.activeProcess = null;
        }
      });

      process.on('error', (err: Error) => {
        console.error('Failed to start Godot process:', err);
        if (this.activeProcess && this.activeProcess.process === process) {
          this.activeProcess = null;
        }
      });

      this.activeProcess = { process, output, errors };

      return {
        content: [
          {
            type: 'text',
            text: `Godot project started in debug mode. Use get_debug_output to see output.`,
          },
        ],
      };
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      return this.createErrorResponse(
        `Failed to run Godot project: ${errorMessage}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the get_debug_output tool
   */
  private async handleGetDebugOutput() {
    if (!this.activeProcess) {
      return this.createErrorResponse(
        'No active Godot process.',
        [
          'Use run_project to start a Godot project first',
          'Check if the Godot process crashed unexpectedly',
        ]
      );
    }

    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify(
            {
              output: this.activeProcess.output,
              errors: this.activeProcess.errors,
            },
            null,
            2
          ),
        },
      ],
    };
  }

  /**
   * Handle the stop_project tool
   */
  private async handleStopProject() {
    if (!this.activeProcess) {
      return this.createErrorResponse(
        'No active Godot process to stop.',
        [
          'Use run_project to start a Godot project first',
          'The process may have already terminated',
        ]
      );
    }

    this.logDebug('Stopping active Godot process');
    this.activeProcess.process.kill();
    const output = this.activeProcess.output;
    const errors = this.activeProcess.errors;
    this.activeProcess = null;

    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify(
            {
              message: 'Godot project stopped',
              finalOutput: output,
              finalErrors: errors,
            },
            null,
            2
          ),
        },
      ],
    };
  }

  /**
   * Handle the get_godot_version tool
   */
  private async handleGetGodotVersion() {
    try {
      // Ensure godotPath is set
      if (!this.godotPath) {
        await this.detectGodotPath();
        if (!this.godotPath) {
          return this.createErrorResponse(
            'Could not find a valid Godot executable path',
            [
              'Ensure Godot is installed correctly',
              'Set GODOT_PATH environment variable to specify the correct path',
            ]
          );
        }
      }

      this.logDebug('Getting Godot version');
      const { stdout } = await execFileAsync(this.godotPath!, ['--version']);
      return {
        content: [
          {
            type: 'text',
            text: stdout.trim(),
          },
        ],
      };
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      return this.createErrorResponse(
        `Failed to get Godot version: ${errorMessage}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
        ]
      );
    }
  }

  /**
   * Handle the list_projects tool
   */
  private async handleListProjects(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.directory) {
      return this.createErrorResponse(
        'Directory is required',
        ['Provide a valid directory path to search for Godot projects']
      );
    }

    if (!this.validatePath(args.directory)) {
      return this.createErrorResponse(
        'Invalid directory path',
        ['Provide a valid path without ".." or other potentially unsafe characters']
      );
    }

    try {
      this.logDebug(`Listing Godot projects in directory: ${args.directory}`);
      if (!existsSync(args.directory)) {
        return this.createErrorResponse(
          `Directory does not exist: ${args.directory}`,
          ['Provide a valid directory path that exists on the system']
        );
      }

      const recursive = args.recursive === true;
      const projects = this.findGodotProjects(args.directory, recursive);

      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(projects, null, 2),
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to list projects: ${error?.message || 'Unknown error'}`,
        [
          'Ensure the directory exists and is accessible',
          'Check if you have permission to read the directory',
        ]
      );
    }
  }

  /**
   * Get the structure of a Godot project asynchronously by counting files recursively
   * @param projectPath Path to the Godot project
   * @returns Promise resolving to an object with counts of scenes, scripts, assets, and other files
   */
  private getProjectStructureAsync(projectPath: string): Promise<any> {
    return new Promise((resolve) => {
      try {
        const structure = {
          scenes: 0,
          scripts: 0,
          assets: 0,
          other: 0,
        };

        const scanDirectory = (currentPath: string) => {
          const entries = readdirSync(currentPath, { withFileTypes: true });
          
          for (const entry of entries) {
            const entryPath = join(currentPath, entry.name);
            
            // Skip hidden files and directories
            if (entry.name.startsWith('.')) {
              continue;
            }
            
            if (entry.isDirectory()) {
              // Recursively scan subdirectories
              scanDirectory(entryPath);
            } else if (entry.isFile()) {
              // Count file by extension
              const ext = entry.name.split('.').pop()?.toLowerCase();
              
              if (ext === 'tscn') {
                structure.scenes++;
              } else if (ext === 'gd' || ext === 'gdscript' || ext === 'cs') {
                structure.scripts++;
              } else if (['png', 'jpg', 'jpeg', 'webp', 'svg', 'ttf', 'wav', 'mp3', 'ogg'].includes(ext || '')) {
                structure.assets++;
              } else {
                structure.other++;
              }
            }
          }
        };
        
        // Start scanning from the project root
        scanDirectory(projectPath);
        resolve(structure);
      } catch (error) {
        this.logDebug(`Error getting project structure asynchronously: ${error}`);
        resolve({ 
          error: 'Failed to get project structure',
          scenes: 0,
          scripts: 0,
          assets: 0,
          other: 0
        });
      }
    });
  }

  /**
   * Handle the get_project_info tool
   */
  private async handleGetProjectInfo(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath) {
      return this.createErrorResponse(
        'Project path is required',
        ['Provide a valid path to a Godot project directory']
      );
    }
  
    if (!this.validatePath(args.projectPath)) {
      return this.createErrorResponse(
        'Invalid project path',
        ['Provide a valid path without ".." or other potentially unsafe characters']
      );
    }
  
    try {
      // Ensure godotPath is set
      if (!this.godotPath) {
        await this.detectGodotPath();
        if (!this.godotPath) {
          return this.createErrorResponse(
            'Could not find a valid Godot executable path',
            [
              'Ensure Godot is installed correctly',
              'Set GODOT_PATH environment variable to specify the correct path',
            ]
          );
        }
      }
  
      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }
  
      this.logDebug(`Getting project info for: ${args.projectPath}`);
  
      // Get Godot version
      const execOptions = { timeout: 10000 }; // 10 second timeout
      const { stdout } = await execFileAsync(this.godotPath!, ['--version'], execOptions);
  
      // Get project structure using the recursive method
      const projectStructure = await this.getProjectStructureAsync(args.projectPath);
  
      // Extract project name from project.godot file
      let projectName = basename(args.projectPath);
      try {
        const fs = require('fs');
        const projectFileContent = fs.readFileSync(projectFile, 'utf8');
        const configNameMatch = projectFileContent.match(/config\/name="([^"]+)"/);
        if (configNameMatch && configNameMatch[1]) {
          projectName = configNameMatch[1];
          this.logDebug(`Found project name in config: ${projectName}`);
        }
      } catch (error) {
        this.logDebug(`Error reading project file: ${error}`);
        // Continue with default project name if extraction fails
      }
  
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(
              {
                name: projectName,
                path: args.projectPath,
                godotVersion: stdout.trim(),
                structure: projectStructure,
              },
              null,
              2
            ),
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to get project info: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the create_scene tool
   */
  private async handleCreateScene(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath || !args.scenePath) {
      return this.createErrorResponse(
        'Project path and scene path are required',
        ['Provide valid paths for both the project and the scene']
      );
    }

    if (!this.validatePath(args.projectPath) || !this.validatePath(args.scenePath)) {
      return this.createErrorResponse(
        'Invalid path',
        ['Provide valid paths without ".." or other potentially unsafe characters']
      );
    }

    const rootNodeType = args.rootNodeType || 'Node2D';
    if (!this.validateClassName(rootNodeType)) {
      return this.createErrorResponse(
        'Invalid rootNodeType',
        ['rootNodeType must be a built-in Godot class name (no paths, no file extensions)']
      );
    }

    try {
      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      // Prepare parameters for the operation (already in camelCase)
      const params = {
        scenePath: args.scenePath,
        rootNodeType,
      };

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('create_scene', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to create scene: ${stderr}`,
          [
            'Check if the root node type is valid',
            'Ensure you have write permissions to the scene path',
            'Verify the scene path is valid',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Scene created successfully at: ${args.scenePath}\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to create scene: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the add_node tool
   */
  private async handleAddNode(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath || !args.scenePath || !args.nodeType || !args.nodeName) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, nodeType, and nodeName']
      );
    }

    if (!this.validatePath(args.projectPath) || !this.validatePath(args.scenePath)) {
      return this.createErrorResponse(
        'Invalid path',
        ['Provide valid paths without ".." or other potentially unsafe characters']
      );
    }

    if (!this.validateClassName(args.nodeType)) {
      return this.createErrorResponse(
        'Invalid nodeType',
        ['nodeType must be a built-in Godot class name (no paths, no file extensions)']
      );
    }

    try {
      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      // Check if the scene file exists
      const scenePath = join(args.projectPath, args.scenePath);
      if (!existsSync(scenePath)) {
        return this.createErrorResponse(
          `Scene file does not exist: ${args.scenePath}`,
          [
            'Ensure the scene path is correct',
            'Use create_scene to create a new scene first',
          ]
        );
      }

      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        nodeType: args.nodeType,
        nodeName: args.nodeName,
      };

      // Add optional parameters
      if (args.parentNodePath) {
        params.parentNodePath = args.parentNodePath;
      }

      if (args.properties) {
        params.properties = args.properties;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('add_node', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to add node: ${stderr}`,
          [
            'Check if the node type is valid',
            'Ensure the parent node path exists',
            'Verify the scene file is valid',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Node '${args.nodeName}' of type '${args.nodeType}' added successfully to '${args.scenePath}'.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to add node: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the load_sprite tool
   */
  private async handleLoadSprite(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath || !args.scenePath || !args.nodePath || !args.texturePath) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, nodePath, and texturePath']
      );
    }

    if (
      !this.validatePath(args.projectPath) ||
      !this.validatePath(args.scenePath) ||
      !this.validatePath(args.nodePath) ||
      !this.validatePath(args.texturePath)
    ) {
      return this.createErrorResponse(
        'Invalid path',
        ['Provide valid paths without ".." or other potentially unsafe characters']
      );
    }

    try {
      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      // Check if the scene file exists
      const scenePath = join(args.projectPath, args.scenePath);
      if (!existsSync(scenePath)) {
        return this.createErrorResponse(
          `Scene file does not exist: ${args.scenePath}`,
          [
            'Ensure the scene path is correct',
            'Use create_scene to create a new scene first',
          ]
        );
      }

      // Check if the texture file exists
      const texturePath = join(args.projectPath, args.texturePath);
      if (!existsSync(texturePath)) {
        return this.createErrorResponse(
          `Texture file does not exist: ${args.texturePath}`,
          [
            'Ensure the texture path is correct',
            'Upload or create the texture file first',
          ]
        );
      }

      // Prepare parameters for the operation (already in camelCase)
      const params = {
        scenePath: args.scenePath,
        nodePath: args.nodePath,
        texturePath: args.texturePath,
      };

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('load_sprite', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to load sprite: ${stderr}`,
          [
            'Check if the node path is correct',
            'Ensure the node is a Sprite2D, Sprite3D, or TextureRect',
            'Verify the texture file is a valid image format',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Sprite loaded successfully with texture: ${args.texturePath}\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to load sprite: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the export_mesh_library tool
   */
  private async handleExportMeshLibrary(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath || !args.scenePath || !args.outputPath) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, and outputPath']
      );
    }

    if (
      !this.validatePath(args.projectPath) ||
      !this.validatePath(args.scenePath) ||
      !this.validatePath(args.outputPath)
    ) {
      return this.createErrorResponse(
        'Invalid path',
        ['Provide valid paths without ".." or other potentially unsafe characters']
      );
    }

    try {
      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      // Check if the scene file exists
      const scenePath = join(args.projectPath, args.scenePath);
      if (!existsSync(scenePath)) {
        return this.createErrorResponse(
          `Scene file does not exist: ${args.scenePath}`,
          [
            'Ensure the scene path is correct',
            'Use create_scene to create a new scene first',
          ]
        );
      }

      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        outputPath: args.outputPath,
      };

      // Add optional parameters
      if (args.meshItemNames && Array.isArray(args.meshItemNames)) {
        params.meshItemNames = args.meshItemNames;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('export_mesh_library', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to export mesh library: ${stderr}`,
          [
            'Check if the scene contains valid 3D meshes',
            'Ensure the output path is valid',
            'Verify the scene file is valid',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `MeshLibrary exported successfully to: ${args.outputPath}\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to export mesh library: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the save_scene tool
   */
  private async handleSaveScene(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath || !args.scenePath) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath and scenePath']
      );
    }

    if (!this.validatePath(args.projectPath) || !this.validatePath(args.scenePath)) {
      return this.createErrorResponse(
        'Invalid path',
        ['Provide valid paths without ".." or other potentially unsafe characters']
      );
    }

    // If newPath is provided, validate it
    if (args.newPath && !this.validatePath(args.newPath)) {
      return this.createErrorResponse(
        'Invalid new path',
        ['Provide a valid new path without ".." or other potentially unsafe characters']
      );
    }

    try {
      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      // Check if the scene file exists
      const scenePath = join(args.projectPath, args.scenePath);
      if (!existsSync(scenePath)) {
        return this.createErrorResponse(
          `Scene file does not exist: ${args.scenePath}`,
          [
            'Ensure the scene path is correct',
            'Use create_scene to create a new scene first',
          ]
        );
      }

      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
      };

      // Add optional parameters
      if (args.newPath) {
        params.newPath = args.newPath;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('save_scene', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to save scene: ${stderr}`,
          [
            'Check if the scene file is valid',
            'Ensure you have write permissions to the output path',
            'Verify the scene can be properly packed',
          ]
        );
      }

      const savePath = args.newPath || args.scenePath;
      return {
        content: [
          {
            type: 'text',
            text: `Scene saved successfully to: ${savePath}\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to save scene: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the get_uid tool
   */
  private async handleGetUid(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath || !args.filePath) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath and filePath']
      );
    }

    if (!this.validatePath(args.projectPath) || !this.validatePath(args.filePath)) {
      return this.createErrorResponse(
        'Invalid path',
        ['Provide valid paths without ".." or other potentially unsafe characters']
      );
    }

    try {
      // Ensure godotPath is set
      if (!this.godotPath) {
        await this.detectGodotPath();
        if (!this.godotPath) {
          return this.createErrorResponse(
            'Could not find a valid Godot executable path',
            [
              'Ensure Godot is installed correctly',
              'Set GODOT_PATH environment variable to specify the correct path',
            ]
          );
        }
      }

      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      // Check if the file exists
      const filePath = join(args.projectPath, args.filePath);
      if (!existsSync(filePath)) {
        return this.createErrorResponse(
          `File does not exist: ${args.filePath}`,
          ['Ensure the file path is correct']
        );
      }

      // Get Godot version to check if UIDs are supported
      const { stdout: versionOutput } = await execFileAsync(this.godotPath!, ['--version']);
      const version = versionOutput.trim();

      if (!this.isGodot44OrLater(version)) {
        return this.createErrorResponse(
          `UIDs are only supported in Godot 4.4 or later. Current version: ${version}`,
          [
            'Upgrade to Godot 4.4 or later to use UIDs',
            'Use resource paths instead of UIDs for this version of Godot',
          ]
        );
      }

      // Prepare parameters for the operation (already in camelCase)
      const params = {
        filePath: args.filePath,
      };

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('get_uid', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to get UID: ${stderr}`,
          [
            'Check if the file is a valid Godot resource',
            'Ensure the file path is correct',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `UID for ${args.filePath}: ${stdout.trim()}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to get UID: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the update_project_uids tool
   */
  private async handleUpdateProjectUids(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);
    
    if (!args.projectPath) {
      return this.createErrorResponse(
        'Project path is required',
        ['Provide a valid path to a Godot project directory']
      );
    }

    if (!this.validatePath(args.projectPath)) {
      return this.createErrorResponse(
        'Invalid project path',
        ['Provide a valid path without ".." or other potentially unsafe characters']
      );
    }

    try {
      // Ensure godotPath is set
      if (!this.godotPath) {
        await this.detectGodotPath();
        if (!this.godotPath) {
          return this.createErrorResponse(
            'Could not find a valid Godot executable path',
            [
              'Ensure Godot is installed correctly',
              'Set GODOT_PATH environment variable to specify the correct path',
            ]
          );
        }
      }

      // Check if the project directory exists and contains a project.godot file
      const projectFile = join(args.projectPath, 'project.godot');
      if (!existsSync(projectFile)) {
        return this.createErrorResponse(
          `Not a valid Godot project: ${args.projectPath}`,
          [
            'Ensure the path points to a directory containing a project.godot file',
            'Use list_projects to find valid Godot projects',
          ]
        );
      }

      // Get Godot version to check if UIDs are supported
      const { stdout: versionOutput } = await execFileAsync(this.godotPath!, ['--version']);
      const version = versionOutput.trim();

      if (!this.isGodot44OrLater(version)) {
        return this.createErrorResponse(
          `UIDs are only supported in Godot 4.4 or later. Current version: ${version}`,
          [
            'Upgrade to Godot 4.4 or later to use UIDs',
            'Use resource paths instead of UIDs for this version of Godot',
          ]
        );
      }

      // Prepare parameters for the operation (already in camelCase)
      const params = {
        projectPath: args.projectPath,
      };

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('resave_resources', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to update project UIDs: ${stderr}`,
          [
            'Check if the project is valid',
            'Ensure you have write permissions to the project directory',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Project UIDs updated successfully.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to update project UIDs: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Shared validation for tools that operate on an existing scene file.
   * Returns an error response, or null if the project and scene are valid.
   */
  private validateProjectAndScene(args: any): any | null {
    if (!this.validatePath(args.projectPath) || !this.validatePath(args.scenePath)) {
      return this.createErrorResponse(
        'Invalid path',
        ['Provide valid paths without ".." or other potentially unsafe characters']
      );
    }

    // Check if the project directory exists and contains a project.godot file
    const projectFile = join(args.projectPath, 'project.godot');
    if (!existsSync(projectFile)) {
      return this.createErrorResponse(
        `Not a valid Godot project: ${args.projectPath}`,
        [
          'Ensure the path points to a directory containing a project.godot file',
          'Use list_projects to find valid Godot projects',
        ]
      );
    }

    // Check if the scene file exists
    const scenePath = join(args.projectPath, args.scenePath);
    if (!existsSync(scenePath)) {
      return this.createErrorResponse(
        `Scene file does not exist: ${args.scenePath}`,
        [
          'Ensure the scene path is correct',
          'Use create_scene to create a new scene first',
        ]
      );
    }

    return null;
  }

  /**
   * Handle the set_lighting tool
   */
  private async handleSetLighting(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.lightType || !args.nodeName) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, lightType, and nodeName']
      );
    }

    const validLightTypes = ['DirectionalLight3D', 'OmniLight3D', 'SpotLight3D'];
    if (!validLightTypes.includes(args.lightType)) {
      return this.createErrorResponse(
        `Invalid lightType: ${args.lightType}`,
        ['Use one of: ' + validLightTypes.join(', ')]
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        lightType: args.lightType,
        nodeName: args.nodeName,
      };

      // Add optional parameters
      if (args.transform) {
        params.transform = args.transform;
      }
      if (args.properties) {
        params.properties = args.properties;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('set_lighting', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to set lighting: ${stderr}`,
          [
            'Check if the light type is valid',
            'Verify the scene file is valid',
            'Ensure transform and properties use valid values',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Light '${args.nodeName}' of type '${args.lightType}' configured in '${args.scenePath}'.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to set lighting: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the place_camera tool
   */
  private async handlePlaceCamera(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.nodeName || !args.position || !args.lookAt) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, nodeName, position, and lookAt']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        nodeName: args.nodeName,
        position: args.position,
        lookAt: args.lookAt,
      };

      // Add optional parameters
      if (args.fov !== undefined) {
        params.fov = args.fov;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('place_camera', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to place camera: ${stderr}`,
          [
            'Verify the scene file is valid',
            'Ensure position and lookAt are valid {x,y,z} points and not identical',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Camera '${args.nodeName}' placed in '${args.scenePath}'.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to place camera: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the build_graybox_set tool
   */
  private async handleBuildGrayboxSet(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !Array.isArray(args.nodes)) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, and a nodes array']
      );
    }

    if (!this.validatePath(args.projectPath) || !this.validatePath(args.scenePath)) {
      return this.createErrorResponse(
        'Invalid path',
        ['Provide valid paths without ".." or other potentially unsafe characters']
      );
    }

    // The scene file is created/overwritten by this tool, so only the project
    // itself must exist (unlike validateProjectAndScene, which requires the scene)
    const projectFile = join(args.projectPath, 'project.godot');
    if (!existsSync(projectFile)) {
      return this.createErrorResponse(
        `Not a valid Godot project: ${args.projectPath}`,
        [
          'Ensure the path points to a directory containing a project.godot file',
          'Use list_projects to find valid Godot projects',
        ]
      );
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        nodes: args.nodes,
      };

      // Add optional parameters
      if (args.camera) {
        params.camera = args.camera;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('build_graybox_set', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to build graybox set: ${stderr}`,
          [
            'Verify every node has a type (box|capsule|sphere|plane|label|light) and a name',
            'Ensure position/size use {x,y,z} and color uses [r,g,b]',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Graybox set built in '${args.scenePath}' (${args.nodes.length} nodes).\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to build graybox set: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the attach_look_at_constraint tool
   */
  private async handleAttachLookAtConstraint(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.sourceNode || !args.targetNode) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, sourceNode, and targetNode']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params = {
        scenePath: args.scenePath,
        sourceNode: args.sourceNode,
        targetNode: args.targetNode,
      };

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('attach_look_at_constraint', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to attach look-at constraint: ${stderr}`,
          [
            'Ensure both nodes exist in the scene',
            'Ensure both nodes are Node3D (or derived) types',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Look-at constraint attached: '${args.sourceNode}' now faces '${args.targetNode}' in '${args.scenePath}'.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to attach look-at constraint: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the create_animation tool
   */
  private async handleCreateAnimation(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.playerNodePath || !args.animationName) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, playerNodePath, and animationName']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        playerNodePath: args.playerNodePath,
        animationName: args.animationName,
      };

      // Add optional parameters
      if (args.length !== undefined) {
        params.length = args.length;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('create_animation', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to create animation: ${stderr}`,
          [
            'Ensure the player node path points to an AnimationPlayer',
            'Verify the scene file is valid',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Animation '${args.animationName}' created on '${args.playerNodePath}' in '${args.scenePath}'.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to create animation: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the add_track tool
   */
  private async handleAddTrack(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.playerNodePath || !args.animationName || !args.trackType || !args.targetNodePath) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, playerNodePath, animationName, trackType, and targetNodePath']
      );
    }

    const validTrackTypes = ['value', 'position_3d', 'rotation_3d', 'scale_3d'];
    if (!validTrackTypes.includes(args.trackType)) {
      return this.createErrorResponse(
        `Invalid trackType: ${args.trackType}`,
        ['Use one of: ' + validTrackTypes.join(', ')]
      );
    }

    if (args.trackType === 'value' && !args.property) {
      return this.createErrorResponse(
        'Missing required parameter: property',
        ['Value tracks require a property name (e.g., "visible")']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        playerNodePath: args.playerNodePath,
        animationName: args.animationName,
        trackType: args.trackType,
        targetNodePath: args.targetNodePath,
      };

      // Add optional parameters
      if (args.property) {
        params.property = args.property;
      }
      if (Array.isArray(args.keyframes)) {
        params.keyframes = args.keyframes;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('add_track', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to add track: ${stderr}`,
          [
            'Ensure the animation exists (use create_animation first)',
            'Ensure the target node path exists in the scene',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Track '${args.trackType}' added to animation '${args.animationName}'.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to add track: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the insert_keyframe tool
   */
  private async handleInsertKeyframe(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.playerNodePath || !args.animationName || args.trackIndex === undefined || args.time === undefined || args.value === undefined) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, playerNodePath, animationName, trackIndex, time, and value']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        playerNodePath: args.playerNodePath,
        animationName: args.animationName,
        trackIndex: args.trackIndex,
        time: args.time,
        value: args.value,
      };

      // Add optional parameters
      if (args.transition !== undefined) {
        params.transition = args.transition;
      }

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('insert_keyframe', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to insert keyframe: ${stderr}`,
          [
            'Ensure the track index is valid (returned by add_track)',
            'Ensure the value type matches the track type (e.g., [x,y,z] for position_3d, [x,y,z,w] quaternion for rotation_3d)',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Keyframe inserted at ${args.time}s on track ${args.trackIndex} of animation '${args.animationName}'.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to insert keyframe: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the set_animation_length tool
   */
  private async handleSetAnimationLength(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.playerNodePath || !args.animationName || args.length === undefined) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, playerNodePath, animationName, and length']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params = {
        scenePath: args.scenePath,
        playerNodePath: args.playerNodePath,
        animationName: args.animationName,
        length: args.length,
      };

      // Execute the operation
      const { stdout, stderr } = await this.executeOperation('set_animation_length', params, args.projectPath);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to set animation length: ${stderr}`,
          ['Ensure the animation exists (use create_animation first)']
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Animation '${args.animationName}' length set to ${args.length}s.\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to set animation length: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the render_viewport tool
   */
  private async handleRenderViewport(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.outputPath) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, and outputPath']
      );
    }

    if (!this.validatePath(args.outputPath)) {
      return this.createErrorResponse(
        'Invalid output path',
        ['Provide a valid output path without ".." or other potentially unsafe characters']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        outputPath: args.outputPath,
      };

      // Add optional parameters
      if (args.resolution) {
        params.resolution = args.resolution;
      }
      if (args.cameraNodePath) {
        params.cameraNodePath = args.cameraNodePath;
      }
      if (args.camera) {
        params.camera = args.camera;
      }
      if (args.hideLabels) {
        params.hideLabels = args.hideLabels;
      }

      // Execute the operation in windowed (non-headless) mode: --headless uses
      // the dummy rasterizer and cannot produce rendered images. The script
      // captures a throwaway SubViewport synchronously via force_draw().
      const { stdout, stderr } = await this.executeOperation('render_viewport', params, args.projectPath, false);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to render viewport: ${stderr}`,
          [
            'Ensure the scene contains a Camera3D (or pass cameraNodePath)',
            'Ensure a display/GPU is available (windowed mode is required)',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Viewport rendered to: ${args.outputPath}\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to render viewport: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the capture_aux_buffer tool
   */
  /**
   * Render many captures of one scene in a single Godot process.
   * See the tool description for why this exists (spawn-per-op startup cost).
   */
  private async handleRenderShotBatch(args: any) {
    if (!Array.isArray(args.shots) || args.shots.length === 0) {
      return this.createErrorResponse(
        'render_shot_batch requires a non-empty "shots" array',
        ['Pass shots: [{shot_id, camera, outputs: {rgb, depth, normal}}]']
      );
    }
    // Every output path lands inside the project, so reuse the same guard the
    // single-capture tools apply — a bad path here would write 246 files loose.
    for (const shot of args.shots) {
      for (const key of ['rgb', 'depth', 'normal']) {
        const p = shot?.outputs?.[key];
        if (p && !this.validatePath(p)) {
          return this.createErrorResponse(
            `Invalid output path in shot ${shot.shot_id}: ${p}`,
            ['Provide paths without ".." or other potentially unsafe characters']
          );
        }
      }
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // convertCamelToSnakeCase deliberately does not recurse into arrays, so
      // the camera specs nested under shots[] would reach GDScript still
      // camelCased and find_render_camera would reject them ("needs position
      // and look_at"). Normalise here rather than changing the global
      // converter, which other array-taking tools (build_graybox_set) rely on.
      const shots = args.shots.map((shot: any) => {
        const cam = shot?.camera;
        if (!cam || cam.look_at || !cam.lookAt) return shot;
        const { lookAt, ...rest } = cam;
        return { ...shot, camera: { ...rest, look_at: lookAt } };
      });

      const params: any = { scenePath: args.scenePath, shots };
      if (args.resolution) {
        params.resolution = args.resolution;
      }
      // Windowed, like the other render ops: --headless has no GPU rasterizer.
      const { stdout, stderr } = await this.executeOperation('render_shot_batch', params, args.projectPath, false);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to render shot batch: ${stderr}`,
          ['Check that every shot has a valid inline camera {position, lookAt}']
        );
      }
      return {
        content: [
          {
            type: 'text',
            text: `Shot batch rendered for ${args.scenePath}\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error) {
      return this.createErrorResponse(
        `Failed to render shot batch: ${error instanceof Error ? error.message : String(error)}`,
        ['Ensure a display/GPU is available (windowed mode is required)']
      );
    }
  }

  private async handleCaptureAuxBuffer(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.outputPath) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, and outputPath']
      );
    }

    if (!this.validatePath(args.outputPath)) {
      return this.createErrorResponse(
        'Invalid output path',
        ['Provide a valid output path without ".." or other potentially unsafe characters']
      );
    }

    const bufferType = args.bufferType || 'depth';
    if (bufferType !== 'depth' && bufferType !== 'normal') {
      return this.createErrorResponse(
        `Invalid bufferType: ${bufferType}`,
        ['Supported buffer types are "depth" and "normal"']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Prepare parameters for the operation (already in camelCase)
      const params: any = {
        scenePath: args.scenePath,
        outputPath: args.outputPath,
        bufferType,
      };

      // Add optional parameters
      if (args.resolution) {
        params.resolution = args.resolution;
      }
      if (args.cameraNodePath) {
        params.cameraNodePath = args.cameraNodePath;
      }
      // Inline camera spec, same as render_viewport. Without this the aux
      // buffer silently falls back to the scene's first Camera3D, producing a
      // frame that does NOT line up with the render_viewport output — which
      // makes the depth/normal channels useless as ControlNet conditioning.
      if (args.camera) {
        params.camera = args.camera;
      }

      // Execute the operation in windowed (non-headless) mode: --headless uses
      // the dummy rasterizer and cannot produce rendered images. The script
      // captures synchronously via force_draw() and quits itself.
      const { stdout, stderr } = await this.executeOperation('capture_aux_buffer', params, args.projectPath, false);

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to capture auxiliary buffer: ${stderr}`,
          [
            'Ensure the scene contains a Camera3D (or pass cameraNodePath)',
            'Ensure a display/GPU is available (windowed mode is required)',
          ]
        );
      }

      return {
        content: [
          {
            type: 'text',
            text: `Auxiliary buffer (${bufferType}) captured to: ${args.outputPath}\n\nOutput: ${stdout}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to capture auxiliary buffer: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Handle the write_movie tool
   */
  private async handleWriteMovie(args: any) {
    // Normalize parameters to camelCase
    args = this.normalizeParameters(args);

    if (!args.projectPath || !args.scenePath || !args.outputPath) {
      return this.createErrorResponse(
        'Missing required parameters',
        ['Provide projectPath, scenePath, and outputPath']
      );
    }

    if (!this.validatePath(args.outputPath)) {
      return this.createErrorResponse(
        'Invalid output path',
        ['Provide a valid output path without ".." or other potentially unsafe characters']
      );
    }

    const format = (args.format || 'avi').toLowerCase();
    if (format !== 'avi' && format !== 'png') {
      return this.createErrorResponse(
        `Invalid format: ${args.format}`,
        ['Use "avi" (MJPEG) or "png" (PNG frame sequence encoded to .mp4 via ffmpeg)']
      );
    }

    const expectedExt = format === 'png' ? '.mp4' : '.avi';
    if (!args.outputPath.toLowerCase().endsWith(expectedExt)) {
      return this.createErrorResponse(
        'Invalid output path',
        [format === 'png' ? 'PNG frame mode encodes to .mp4; use an output path ending with .mp4' : 'Godot Movie Maker mode only writes .avi (MJPEG) files']
      );
    }

    const validationError = this.validateProjectAndScene(args);
    if (validationError) {
      return validationError;
    }

    try {
      // Ensure godotPath is set
      if (!this.godotPath) {
        await this.detectGodotPath();
        if (!this.godotPath) {
          return this.createErrorResponse(
            'Could not find a valid Godot executable path',
            [
              'Ensure Godot is installed correctly',
              'Set GODOT_PATH environment variable to specify the correct path',
            ]
          );
        }
      }

      const fps = Math.max(1, Math.round(args.fps || 30));
      const durationSec = Math.max(0.1, Number(args.durationSec || 5));
      // Small margin so startup iterations don't eat recorded frames
      const frames = Math.max(1, Math.round(durationSec * fps) + 5);

      // Resolve the output path against the project directory
      const outputAbs = join(args.projectPath, args.outputPath);

      // PNG frame mode (same approach as godogen): Godot Movie Maker writes a
      // numbered PNG frame sequence when the --write-movie path ends in .png;
      // the frames are then encoded to .mp4 with ffmpeg below.
      const isPngFormat = format === 'png';
      const framesDir = outputAbs + '.frames';
      const movieFileAbs = isPngFormat ? join(framesDir, 'frame.png') : outputAbs;
      if (isPngFormat) {
        rmSync(framesDir, { recursive: true, force: true });
      }
      mkdirSync(dirname(movieFileAbs), { recursive: true });

      // Movie Maker mode requires a real rendering driver, so run windowed
      // (no --headless) and quit after the requested number of fixed-fps frames.
      const godotArgs = [
        '--path',
        args.projectPath,
        '--fixed-fps',
        String(fps),
        '--write-movie',
        movieFileAbs,
        '--quit-after',
        String(frames),
        args.scenePath,
      ];

      this.logDebug(`Executing: ${this.godotPath} ${godotArgs.join(' ')}`);

      let stdout = '';
      let stderr = '';
      try {
        const result = await execFileAsync(this.godotPath, godotArgs, { maxBuffer: 32 * 1024 * 1024 });
        stdout = result.stdout ?? '';
        stderr = result.stderr ?? '';
      } catch (error: unknown) {
        // If execFileAsync throws, it still contains stdout/stderr
        if (error instanceof Error && 'stdout' in error && 'stderr' in error) {
          const execError = error as Error & { stdout: string; stderr: string; message: string };
          return this.createErrorResponse(
            `Failed to write movie: ${execError.message}\n${execError.stderr ?? ''}`,
            [
              'Ensure a display/GPU is available (windowed mode is required)',
              'Ensure the output path extension matches the requested format (.avi or .mp4)',
              'Verify the scene can be run by Godot',
            ]
          );
        }
        throw error;
      }

      if (stderr && stderr.includes('Failed to')) {
        return this.createErrorResponse(
          `Failed to write movie: ${stderr}`,
          ['Verify the scene can be run by Godot']
        );
      }

      if (!isPngFormat && !existsSync(outputAbs)) {
        return this.createErrorResponse(
          `Failed to write movie: output file was not created at ${outputAbs}`,
          [
            'Ensure a display/GPU is available (windowed mode is required)',
            'Check Godot output: ' + stdout.slice(-500),
          ]
        );
      }

      if (isPngFormat) {
        // PNG frame mode: encode the frame sequence to .mp4 with ffmpeg
        const frameFiles = existsSync(framesDir)
          ? readdirSync(framesDir).filter((f) => /^frame\d+\.png$/.test(f)).sort()
          : [];
        if (frameFiles.length === 0) {
          return this.createErrorResponse(
            `Failed to write movie: no PNG frames were created in ${framesDir}`,
            [
              'Ensure a display/GPU is available (windowed mode is required)',
              'Check Godot output: ' + stdout.slice(-500),
            ]
          );
        }

        const frameDigits = frameFiles[0].match(/^frame(\d+)\.png$/)![1].length;
        const framePattern = join(framesDir, `frame%0${frameDigits}d.png`);
        const ffmpegArgs = [
          '-y',
          '-framerate', String(fps),
          '-i', framePattern,
          '-c:v', 'libx264',
          '-pix_fmt', 'yuv420p',
          outputAbs,
        ];
        this.logDebug(`Executing: ffmpeg ${ffmpegArgs.join(' ')}`);
        try {
          await execFileAsync('ffmpeg', ffmpegArgs, { maxBuffer: 32 * 1024 * 1024 });
        } catch (error: unknown) {
          return this.createErrorResponse(
            `Failed to encode PNG frames to mp4: ${error instanceof Error ? error.message : 'Unknown error'}`,
            ['Ensure ffmpeg is installed and available in PATH']
          );
        }

        // Remove the temporary frame sequence
        rmSync(framesDir, { recursive: true, force: true });

        if (!existsSync(outputAbs)) {
          return this.createErrorResponse(
            `Failed to write movie: ffmpeg did not create ${outputAbs}`,
            ['Ensure ffmpeg is installed and available in PATH']
          );
        }
      }

      return {
        content: [
          {
            type: 'text',
            text: `Movie written to: ${args.outputPath} (${durationSec}s at ${fps} fps, ${isPngFormat ? 'PNG frames -> H.264/.mp4' : 'MJPEG/.avi'})\n\nOutput: ${stdout.slice(-500)}`,
          },
        ],
      };
    } catch (error: any) {
      return this.createErrorResponse(
        `Failed to write movie: ${error?.message || 'Unknown error'}`,
        [
          'Ensure Godot is installed correctly',
          'Check if the GODOT_PATH environment variable is set correctly',
          'Verify the project path is accessible',
        ]
      );
    }
  }

  /**
   * Run the MCP server
   */
  async run() {
    try {
      // Detect Godot path before starting the server
      await this.detectGodotPath();

      if (!this.godotPath) {
        console.error('[SERVER] Failed to find a valid Godot executable path');
        console.error('[SERVER] Please set GODOT_PATH environment variable or provide a valid path');
        process.exit(1);
      }

      // Check if the path is valid
      const isValid = await this.isValidGodotPath(this.godotPath);

      if (!isValid) {
        if (this.strictPathValidation) {
          // In strict mode, exit if the path is invalid
          console.error(`[SERVER] Invalid Godot path: ${this.godotPath}`);
          console.error('[SERVER] Please set a valid GODOT_PATH environment variable or provide a valid path');
          process.exit(1);
        } else {
          // In compatibility mode, warn but continue with the default path
          console.error(`[SERVER] Warning: Using potentially invalid Godot path: ${this.godotPath}`);
          console.error('[SERVER] This may cause issues when executing Godot commands');
          console.error('[SERVER] This fallback behavior will be removed in a future version. Set strictPathValidation: true to opt-in to the new behavior.');
        }
      }

      console.error(`[SERVER] Using Godot at: ${this.godotPath}`);

      const transport = new StdioServerTransport();
      await this.server.connect(transport);
      console.error('Godot MCP server running on stdio');
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      console.error('[SERVER] Failed to start:', errorMessage);
      process.exit(1);
    }
  }
}

// Create and run the server
const server = new GodotServer();
server.run().catch((error: unknown) => {
  const errorMessage = error instanceof Error ? error.message : 'Unknown error';
  console.error('Failed to run server:', errorMessage);
  process.exit(1);
});
