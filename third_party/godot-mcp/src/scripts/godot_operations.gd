#!/usr/bin/env -S godot --headless --script
extends SceneTree

# Debug mode flag
var debug_mode = false

# Deferred render state for render_viewport / capture_aux_buffer (windowed only).
# During _init of a --script run the SceneTree is not active yet, so nodes
# added here are not "inside the tree" and cannot be rendered; capture is
# deferred to the first frame instead. null means no render is pending;
# otherwise holds [operation, params].
var pending_render = null

# Warm-up frame counter: the capture must NOT happen on the very first frame.
# On frame 1 the window/viewport state is still initializing (stale transforms,
# viewport size not yet applied), which produced cropped/distorted captures.
var render_warmup_frames = 0

# Second deferred state: a render whose scene + throwaway SubViewport are
# already set up, waiting for the actual pixel capture. A SubViewport and
# camera created and force-drawn in the SAME frame render with stale
# (identity) camera transforms, so setup and capture run a few frames apart.
# Holds [kind ("render"|"aux"), sub_viewport, absolute_output_path, scene_root].
var pending_capture = null
var capture_wait_frames = 0

# Which auxiliary buffer the pending "aux" capture is producing ("depth"|"normal").
# Kept separate from pending_capture so the shared 4-element layout is unchanged.
var pending_buffer_type = "depth"

# --- render_shot_batch state ------------------------------------------------
# spawn-per-op means one Godot process per capture: for an 82-shot × 3-channel
# previs export that is 246 process launches, and ~1.5 s of every ~2 s is pure
# engine startup. render_shot_batch loads the scene ONCE and walks a queue of
# captures inside a single process, cutting the launches to one per scene.
# batch_jobs holds the remaining captures; null batch_scene_root = not batching.
var batch_jobs = []
var batch_scene_root = null
var batch_results = []
var batch_meshes = []

# Called every frame by the main loop. Only used by render_viewport and
# capture_aux_buffer: after a few warm-up frames the scene tree and viewport
# are fully active, so the throwaway SubViewport can be set up; the capture
# itself is deferred again (see pending_capture above).
func _process(_delta):
    if pending_capture != null:
        capture_wait_frames += 1
        if capture_wait_frames < 2:
            return false
        finish_render_capture()
        return false
    if pending_render == null:
        return false
    render_warmup_frames += 1
    if render_warmup_frames < 2:
        return false
    var operation = pending_render[0]
    var params = pending_render[1]
    pending_render = null
    if operation == "capture_aux_buffer":
        capture_aux_buffer(params)
    elif operation == "render_shot_batch":
        render_shot_batch(params)
    else:
        render_viewport(params)
    return false

func _init():
    var args = OS.get_cmdline_args()
    
    # Check for debug flag
    debug_mode = "--debug-godot" in args
    
    # Find the script argument and determine the positions of operation and params
    var script_index = args.find("--script")
    if script_index == -1:
        log_error("Could not find --script argument")
        quit(1)
    
    # The operation should be 2 positions after the script path (script_index + 1 is the script path itself)
    var operation_index = script_index + 2
    # The params should be 3 positions after the script path
    var params_index = script_index + 3
    
    if args.size() <= params_index:
        log_error("Usage: godot --headless --script godot_operations.gd <operation> <json_params>")
        log_error("Not enough command-line arguments provided.")
        quit(1)
    
    # Log all arguments for debugging
    log_debug("All arguments: " + str(args))
    log_debug("Script index: " + str(script_index))
    log_debug("Operation index: " + str(operation_index))
    log_debug("Params index: " + str(params_index))
    
    var operation = args[operation_index]
    var params_json = args[params_index]
    
    log_info("Operation: " + operation)
    log_debug("Params JSON: " + params_json)
    
    # Parse JSON using Godot 4.x API
    var json = JSON.new()
    var error = json.parse(params_json)
    var params = null
    
    if error == OK:
        params = json.get_data()
    else:
        log_error("Failed to parse JSON parameters: " + params_json)
        log_error("JSON Error: " + json.get_error_message() + " at line " + str(json.get_error_line()))
        quit(1)
    
    if not params:
        log_error("Failed to parse JSON parameters: " + params_json)
        quit(1)
    
    log_info("Executing operation: " + operation)
    
    match operation:
        "create_scene":
            create_scene(params)
        "add_node":
            add_node(params)
        "load_sprite":
            load_sprite(params)
        "export_mesh_library":
            export_mesh_library(params)
        "save_scene":
            save_scene(params)
        "get_uid":
            get_uid(params)
        "resave_resources":
            resave_resources(params)
        "set_lighting":
            set_lighting(params)
        "place_camera":
            place_camera(params)
        "build_graybox_set":
            build_graybox_set(params)
        "attach_look_at_constraint":
            attach_look_at_constraint(params)
        "create_animation":
            create_animation(params)
        "add_track":
            add_track(params)
        "insert_keyframe":
            insert_keyframe(params)
        "set_animation_length":
            set_animation_length(params)
        "render_viewport":
            # Deferred to the first frame: during _init the SceneTree is not
            # active yet, so the operation must not run (or quit) here.
            pending_render = [operation, params]
            return
        "capture_aux_buffer":
            pending_render = [operation, params]
            return
        "render_shot_batch":
            pending_render = [operation, params]
            return
        _:
            log_error("Unknown operation: " + operation)
            quit(1)
    
    quit()

# Logging functions
func log_debug(message):
    if debug_mode:
        print("[DEBUG] " + message)

func log_info(message):
    print("[INFO] " + message)

func log_error(message):
    printerr("[ERROR] " + message)

# Get a script by registered class name.
# Only looks up names via the project's global class registry. Raw paths
# (e.g. "res://evil.gd") are intentionally not accepted here to prevent
# arbitrary script instantiation from agent-supplied input.
func get_script_by_name(name_of_class):
    if debug_mode:
        print("Attempting to get script for class: " + name_of_class)

    # Search for it in the global class registry if it's a class name
    var global_classes = ProjectSettings.get_global_class_list()
    if debug_mode:
        print("Searching through " + str(global_classes.size()) + " global classes")
    
    for global_class in global_classes:
        var found_name_of_class = global_class["class"]
        var found_path = global_class["path"]
        
        if found_name_of_class == name_of_class:
            if debug_mode:
                print("Found matching class in registry: " + found_name_of_class + " at path: " + found_path)
            var script = load(found_path) as Script
            if script:
                if debug_mode:
                    print("Successfully loaded script from registry")
                return script
            else:
                printerr("Failed to load script from registry path: " + found_path)
                break
    
    printerr("Could not find script for class: " + name_of_class)
    return null

# Instantiate a class by name
func instantiate_class(name_of_class):
    if name_of_class.is_empty():
        printerr("Cannot instantiate class: name is empty")
        return null
    
    var result = null
    if debug_mode:
        print("Attempting to instantiate class: " + name_of_class)
    
    # Check if it's a built-in class
    if ClassDB.class_exists(name_of_class):
        if debug_mode:
            print("Class exists in ClassDB, using ClassDB.instantiate()")
        if ClassDB.can_instantiate(name_of_class):
            result = ClassDB.instantiate(name_of_class)
            if result == null:
                printerr("ClassDB.instantiate() returned null for class: " + name_of_class)
        else:
            printerr("Class exists but cannot be instantiated: " + name_of_class)
            printerr("This may be an abstract class or interface that cannot be directly instantiated")
    else:
        # Try to get the script
        if debug_mode:
            print("Class not found in ClassDB, trying to get script")
        var script = get_script_by_name(name_of_class)
        if script is GDScript:
            if debug_mode:
                print("Found GDScript, creating instance")
            result = script.new()
        else:
            printerr("Failed to get script for class: " + name_of_class)
            return null
    
    if result == null:
        printerr("Failed to instantiate class: " + name_of_class)
    elif debug_mode:
        print("Successfully instantiated class: " + name_of_class + " of type: " + result.get_class())
    
    return result

# Create a new scene with a specified root node type
func create_scene(params):
    print("Creating scene: " + params.scene_path)
    
    # Get project paths and log them for debugging
    var project_res_path = "res://"
    var project_user_path = "user://"
    var global_res_path = ProjectSettings.globalize_path(project_res_path)
    var global_user_path = ProjectSettings.globalize_path(project_user_path)
    
    if debug_mode:
        print("Project paths:")
        print("- res:// path: " + project_res_path)
        print("- user:// path: " + project_user_path)
        print("- Globalized res:// path: " + global_res_path)
        print("- Globalized user:// path: " + global_user_path)
        
        # Print some common environment variables for debugging
        print("Environment variables:")
        var env_vars = ["PATH", "HOME", "USER", "TEMP", "GODOT_PATH"]
        for env_var in env_vars:
            if OS.has_environment(env_var):
                print("  " + env_var + " = " + OS.get_environment(env_var))
    
    # Normalize the scene path
    var full_scene_path = params.scene_path
    if not full_scene_path.begins_with("res://"):
        full_scene_path = "res://" + full_scene_path
    if debug_mode:
        print("Scene path (with res://): " + full_scene_path)
    
    # Convert resource path to an absolute path
    var absolute_scene_path = ProjectSettings.globalize_path(full_scene_path)
    if debug_mode:
        print("Absolute scene path: " + absolute_scene_path)
    
    # Get the scene directory paths
    var scene_dir_res = full_scene_path.get_base_dir()
    var scene_dir_abs = absolute_scene_path.get_base_dir()
    if debug_mode:
        print("Scene directory (resource path): " + scene_dir_res)
        print("Scene directory (absolute path): " + scene_dir_abs)
    
    # Only do extensive testing in debug mode
    if debug_mode:
        # Try to create a simple test file in the project root to verify write access
        var initial_test_file_path = "res://godot_mcp_test_write.tmp"
        var initial_test_file = FileAccess.open(initial_test_file_path, FileAccess.WRITE)
        if initial_test_file:
            initial_test_file.store_string("Test write access")
            initial_test_file.close()
            print("Successfully wrote test file to project root: " + initial_test_file_path)
            
            # Verify the test file exists
            var initial_test_file_exists = FileAccess.file_exists(initial_test_file_path)
            print("Test file exists check: " + str(initial_test_file_exists))
            
            # Clean up the test file
            if initial_test_file_exists:
                var remove_error = DirAccess.remove_absolute(ProjectSettings.globalize_path(initial_test_file_path))
                print("Test file removal result: " + str(remove_error))
        else:
            var write_error = FileAccess.get_open_error()
            printerr("Failed to write test file to project root: " + str(write_error))
            printerr("This indicates a serious permission issue with the project directory")
    
    # Use traditional if-else statement for better compatibility
    var root_node_type = "Node2D"  # Default value
    if params.has("root_node_type"):
        root_node_type = params.root_node_type
    if debug_mode:
        print("Root node type: " + root_node_type)
    
    # Create the root node
    var scene_root = instantiate_class(root_node_type)
    if not scene_root:
        printerr("Failed to instantiate node of type: " + root_node_type)
        printerr("Make sure the class exists and can be instantiated")
        printerr("Check if the class is registered in ClassDB or available as a script")
        quit(1)
    
    scene_root.name = "root"
    if debug_mode:
        print("Root node created with name: " + scene_root.name)
    
    # Set the owner of the root node to itself (important for scene saving)
    scene_root.owner = scene_root
    
    # Pack the scene
    var packed_scene = PackedScene.new()
    var result = packed_scene.pack(scene_root)
    if debug_mode:
        print("Pack result: " + str(result) + " (OK=" + str(OK) + ")")
    
    if result == OK:
        # Only do extensive testing in debug mode
        if debug_mode:
            # First, let's verify we can write to the project directory
            print("Testing write access to project directory...")
            var test_write_path = "res://test_write_access.tmp"
            var test_write_abs = ProjectSettings.globalize_path(test_write_path)
            var test_file = FileAccess.open(test_write_path, FileAccess.WRITE)
            
            if test_file:
                test_file.store_string("Write test")
                test_file.close()
                print("Successfully wrote test file to project directory")
                
                # Clean up test file
                if FileAccess.file_exists(test_write_path):
                    var remove_error = DirAccess.remove_absolute(test_write_abs)
                    print("Test file removal result: " + str(remove_error))
            else:
                var write_error = FileAccess.get_open_error()
                printerr("Failed to write test file to project directory: " + str(write_error))
                printerr("This may indicate permission issues with the project directory")
                # Continue anyway, as the scene directory might still be writable
        
        # Ensure the scene directory exists using DirAccess
        if debug_mode:
            print("Ensuring scene directory exists...")
        
        # Get the scene directory relative to res://
        var scene_dir_relative = scene_dir_res.substr(6)  # Remove "res://" prefix
        if debug_mode:
            print("Scene directory (relative to res://): " + scene_dir_relative)
        
        # Create the directory if needed
        if not scene_dir_relative.is_empty():
            # First check if it exists
            var dir_exists = DirAccess.dir_exists_absolute(scene_dir_abs)
            if debug_mode:
                print("Directory exists check (absolute): " + str(dir_exists))
            
            if not dir_exists:
                if debug_mode:
                    print("Directory doesn't exist, creating: " + scene_dir_relative)
                
                # Try to create the directory using DirAccess
                var dir = DirAccess.open("res://")
                if dir == null:
                    var open_error = DirAccess.get_open_error()
                    printerr("Failed to open res:// directory: " + str(open_error))
                    
                    # Try alternative approach with absolute path
                    if debug_mode:
                        print("Trying alternative directory creation approach...")
                    var make_dir_error = DirAccess.make_dir_recursive_absolute(scene_dir_abs)
                    if debug_mode:
                        print("Make directory result (absolute): " + str(make_dir_error))
                    
                    if make_dir_error != OK:
                        printerr("Failed to create directory using absolute path")
                        printerr("Error code: " + str(make_dir_error))
                        quit(1)
                else:
                    # Create the directory using the DirAccess instance
                    if debug_mode:
                        print("Creating directory using DirAccess: " + scene_dir_relative)
                    var make_dir_error = dir.make_dir_recursive(scene_dir_relative)
                    if debug_mode:
                        print("Make directory result: " + str(make_dir_error))
                    
                    if make_dir_error != OK:
                        printerr("Failed to create directory: " + scene_dir_relative)
                        printerr("Error code: " + str(make_dir_error))
                        quit(1)
                
                # Verify the directory was created
                dir_exists = DirAccess.dir_exists_absolute(scene_dir_abs)
                if debug_mode:
                    print("Directory exists check after creation: " + str(dir_exists))
                
                if not dir_exists:
                    printerr("Directory reported as created but does not exist: " + scene_dir_abs)
                    printerr("This may indicate a problem with path resolution or permissions")
                    quit(1)
            elif debug_mode:
                print("Directory already exists: " + scene_dir_abs)
        
        # Save the scene
        if debug_mode:
            print("Saving scene to: " + full_scene_path)
        var save_error = ResourceSaver.save(packed_scene, full_scene_path)
        if debug_mode:
            print("Save result: " + str(save_error) + " (OK=" + str(OK) + ")")
        
        if save_error == OK:
            # Only do extensive testing in debug mode
            if debug_mode:
                # Wait a moment to ensure file system has time to complete the write
                print("Waiting for file system to complete write operation...")
                OS.delay_msec(500)  # 500ms delay
                
                # Verify the file was actually created using multiple methods
                var file_check_abs = FileAccess.file_exists(absolute_scene_path)
                print("File exists check (absolute path): " + str(file_check_abs))
                
                var file_check_res = FileAccess.file_exists(full_scene_path)
                print("File exists check (resource path): " + str(file_check_res))
                
                var res_exists = ResourceLoader.exists(full_scene_path)
                print("Resource exists check: " + str(res_exists))
                
                # If file doesn't exist by absolute path, try to create a test file in the same directory
                if not file_check_abs and not file_check_res:
                    printerr("Scene file not found after save. Trying to diagnose the issue...")
                    
                    # Try to write a test file to the same directory
                    var test_scene_file_path = scene_dir_res + "/test_scene_file.tmp"
                    var test_scene_file = FileAccess.open(test_scene_file_path, FileAccess.WRITE)
                    
                    if test_scene_file:
                        test_scene_file.store_string("Test scene directory write")
                        test_scene_file.close()
                        print("Successfully wrote test file to scene directory: " + test_scene_file_path)
                        
                        # Check if the test file exists
                        var test_file_exists = FileAccess.file_exists(test_scene_file_path)
                        print("Test file exists: " + str(test_file_exists))
                        
                        if test_file_exists:
                            # Directory is writable, so the issue is with scene saving
                            printerr("Directory is writable but scene file wasn't created.")
                            printerr("This suggests an issue with ResourceSaver.save() or the packed scene.")
                            
                            # Try saving with a different approach
                            print("Trying alternative save approach...")
                            var alt_save_error = ResourceSaver.save(packed_scene, test_scene_file_path + ".tscn")
                            print("Alternative save result: " + str(alt_save_error))
                            
                            # Clean up test files
                            DirAccess.remove_absolute(ProjectSettings.globalize_path(test_scene_file_path))
                            if alt_save_error == OK:
                                DirAccess.remove_absolute(ProjectSettings.globalize_path(test_scene_file_path + ".tscn"))
                        else:
                            printerr("Test file couldn't be verified. This suggests filesystem access issues.")
                    else:
                        var write_error = FileAccess.get_open_error()
                        printerr("Failed to write test file to scene directory: " + str(write_error))
                        printerr("This confirms there are permission or path issues with the scene directory.")
                    
                    # Return error since we couldn't create the scene file
                    printerr("Failed to create scene: " + params.scene_path)
                    quit(1)
                
                # If we get here, at least one of our file checks passed
                if file_check_abs or file_check_res or res_exists:
                    print("Scene file verified to exist!")
                    
                    # Try to load the scene to verify it's valid
                    var test_load = ResourceLoader.load(full_scene_path)
                    if test_load:
                        print("Scene created and verified successfully at: " + params.scene_path)
                        print("Scene file can be loaded correctly.")
                    else:
                        print("Scene file exists but cannot be loaded. It may be corrupted or incomplete.")
                        # Continue anyway since the file exists
                    
                    print("Scene created successfully at: " + params.scene_path)
                else:
                    printerr("All file existence checks failed despite successful save operation.")
                    printerr("This indicates a serious issue with file system access or path resolution.")
                    quit(1)
            else:
                # In non-debug mode, just check if the file exists
                var file_exists = FileAccess.file_exists(full_scene_path)
                if file_exists:
                    print("Scene created successfully at: " + params.scene_path)
                else:
                    printerr("Failed to create scene: " + params.scene_path)
                    quit(1)
        else:
            # Handle specific error codes
            var error_message = "Failed to save scene. Error code: " + str(save_error)
            
            if save_error == ERR_CANT_CREATE:
                error_message += " (ERR_CANT_CREATE - Cannot create the scene file)"
            elif save_error == ERR_CANT_OPEN:
                error_message += " (ERR_CANT_OPEN - Cannot open the scene file for writing)"
            elif save_error == ERR_FILE_CANT_WRITE:
                error_message += " (ERR_FILE_CANT_WRITE - Cannot write to the scene file)"
            elif save_error == ERR_FILE_NO_PERMISSION:
                error_message += " (ERR_FILE_NO_PERMISSION - No permission to write the scene file)"
            
            printerr(error_message)
            quit(1)
    else:
        printerr("Failed to pack scene: " + str(result))
        printerr("Error code: " + str(result))
        quit(1)

# Add a node to an existing scene
func add_node(params):
    print("Adding node to scene: " + params.scene_path)
    
    var full_scene_path = params.scene_path
    if not full_scene_path.begins_with("res://"):
        full_scene_path = "res://" + full_scene_path
    if debug_mode:
        print("Scene path (with res://): " + full_scene_path)
    
    var absolute_scene_path = ProjectSettings.globalize_path(full_scene_path)
    if debug_mode:
        print("Absolute scene path: " + absolute_scene_path)
    
    if not FileAccess.file_exists(absolute_scene_path):
        printerr("Scene file does not exist at: " + absolute_scene_path)
        quit(1)
    
    var scene = load(full_scene_path)
    if not scene:
        printerr("Failed to load scene: " + full_scene_path)
        quit(1)
    
    if debug_mode:
        print("Scene loaded successfully")
    var scene_root = scene.instantiate()
    if debug_mode:
        print("Scene instantiated")
    
    # Use traditional if-else statement for better compatibility
    var parent_path = "root"  # Default value
    if params.has("parent_node_path"):
        parent_path = params.parent_node_path
    if debug_mode:
        print("Parent path: " + parent_path)
    
    var parent = scene_root
    if parent_path != "root":
        parent = scene_root.get_node(parent_path.replace("root/", ""))
        if not parent:
            printerr("Parent node not found: " + parent_path)
            quit(1)
    if debug_mode:
        print("Parent node found: " + parent.name)
    
    if debug_mode:
        print("Instantiating node of type: " + params.node_type)
    var new_node = instantiate_class(params.node_type)
    if not new_node:
        printerr("Failed to instantiate node of type: " + params.node_type)
        printerr("Make sure the class exists and can be instantiated")
        printerr("Check if the class is registered in ClassDB or available as a script")
        quit(1)
    new_node.name = params.node_name
    if debug_mode:
        print("New node created with name: " + new_node.name)
    
    if params.has("properties"):
        if debug_mode:
            print("Setting properties on node")
        var properties = params.properties
        for property in properties:
            if debug_mode:
                print("Setting property: " + property + " = " + str(properties[property]))
            var value = properties[property]
            if typeof(value) == TYPE_STRING and value.begins_with("res://"):
                value = load(value)
                if debug_mode:
                    print("Loaded resource for property: " + property + " -> " + str(value))
            new_node.set(property, value)
    
    parent.add_child(new_node)
    new_node.owner = scene_root
    if debug_mode:
        print("Node added to parent and ownership set")
    
    var packed_scene = PackedScene.new()
    var result = packed_scene.pack(scene_root)
    if debug_mode:
        print("Pack result: " + str(result) + " (OK=" + str(OK) + ")")
    
    if result == OK:
        if debug_mode:
            print("Saving scene to: " + absolute_scene_path)
        var save_error = ResourceSaver.save(packed_scene, absolute_scene_path)
        if debug_mode:
            print("Save result: " + str(save_error) + " (OK=" + str(OK) + ")")
        if save_error == OK:
            if debug_mode:
                var file_check_after = FileAccess.file_exists(absolute_scene_path)
                print("File exists check after save: " + str(file_check_after))
                if file_check_after:
                    print("Node '" + params.node_name + "' of type '" + params.node_type + "' added successfully")
                else:
                    printerr("File reported as saved but does not exist at: " + absolute_scene_path)
            else:
                print("Node '" + params.node_name + "' of type '" + params.node_type + "' added successfully")
        else:
            printerr("Failed to save scene: " + str(save_error))
    else:
        printerr("Failed to pack scene: " + str(result))

# Load a sprite into a Sprite2D node
func load_sprite(params):
    print("Loading sprite into scene: " + params.scene_path)
    
    # Ensure the scene path starts with res:// for Godot's resource system
    var full_scene_path = params.scene_path
    if not full_scene_path.begins_with("res://"):
        full_scene_path = "res://" + full_scene_path
    
    if debug_mode:
        print("Full scene path (with res://): " + full_scene_path)
    
    # Check if the scene file exists
    var file_check = FileAccess.file_exists(full_scene_path)
    if debug_mode:
        print("Scene file exists check: " + str(file_check))
    
    if not file_check:
        printerr("Scene file does not exist at: " + full_scene_path)
        # Get the absolute path for reference
        var absolute_path = ProjectSettings.globalize_path(full_scene_path)
        printerr("Absolute file path that doesn't exist: " + absolute_path)
        quit(1)
    
    # Ensure the texture path starts with res:// for Godot's resource system
    var full_texture_path = params.texture_path
    if not full_texture_path.begins_with("res://"):
        full_texture_path = "res://" + full_texture_path
    
    if debug_mode:
        print("Full texture path (with res://): " + full_texture_path)
    
    # Load the scene
    var scene = load(full_scene_path)
    if not scene:
        printerr("Failed to load scene: " + full_scene_path)
        quit(1)
    
    if debug_mode:
        print("Scene loaded successfully")
    
    # Instance the scene
    var scene_root = scene.instantiate()
    if debug_mode:
        print("Scene instantiated")
    
    # Find the sprite node
    var node_path = params.node_path
    if debug_mode:
        print("Original node path: " + node_path)
    
    if node_path.begins_with("root/"):
        node_path = node_path.substr(5)  # Remove "root/" prefix
        if debug_mode:
            print("Node path after removing 'root/' prefix: " + node_path)
    
    var sprite_node = null
    if node_path == "":
        # If no node path, assume root is the sprite
        sprite_node = scene_root
        if debug_mode:
            print("Using root node as sprite node")
    else:
        sprite_node = scene_root.get_node(node_path)
        if sprite_node and debug_mode:
            print("Found sprite node: " + sprite_node.name)
    
    if not sprite_node:
        printerr("Node not found: " + params.node_path)
        quit(1)
    
    # Check if the node is a Sprite2D or compatible type
    if debug_mode:
        print("Node class: " + sprite_node.get_class())
    if not (sprite_node is Sprite2D or sprite_node is Sprite3D or sprite_node is TextureRect):
        printerr("Node is not a sprite-compatible type: " + sprite_node.get_class())
        quit(1)
    
    # Load the texture
    if debug_mode:
        print("Loading texture from: " + full_texture_path)
    var texture = load(full_texture_path)
    if not texture:
        printerr("Failed to load texture: " + full_texture_path)
        quit(1)
    
    if debug_mode:
        print("Texture loaded successfully")
    
    # Set the texture on the sprite
    if sprite_node is Sprite2D or sprite_node is Sprite3D:
        sprite_node.texture = texture
        if debug_mode:
            print("Set texture on Sprite2D/Sprite3D node")
    elif sprite_node is TextureRect:
        sprite_node.texture = texture
        if debug_mode:
            print("Set texture on TextureRect node")
    
    # Save the modified scene
    var packed_scene = PackedScene.new()
    var result = packed_scene.pack(scene_root)
    if debug_mode:
        print("Pack result: " + str(result) + " (OK=" + str(OK) + ")")
    
    if result == OK:
        if debug_mode:
            print("Saving scene to: " + full_scene_path)
        var error = ResourceSaver.save(packed_scene, full_scene_path)
        if debug_mode:
            print("Save result: " + str(error) + " (OK=" + str(OK) + ")")
        
        if error == OK:
            # Verify the file was actually updated
            if debug_mode:
                var file_check_after = FileAccess.file_exists(full_scene_path)
                print("File exists check after save: " + str(file_check_after))
                
                if file_check_after:
                    print("Sprite loaded successfully with texture: " + full_texture_path)
                    # Get the absolute path for reference
                    var absolute_path = ProjectSettings.globalize_path(full_scene_path)
                    print("Absolute file path: " + absolute_path)
                else:
                    printerr("File reported as saved but does not exist at: " + full_scene_path)
            else:
                print("Sprite loaded successfully with texture: " + full_texture_path)
        else:
            printerr("Failed to save scene: " + str(error))
    else:
        printerr("Failed to pack scene: " + str(result))

# Export a scene as a MeshLibrary resource
func export_mesh_library(params):
    print("Exporting MeshLibrary from scene: " + params.scene_path)
    
    # Ensure the scene path starts with res:// for Godot's resource system
    var full_scene_path = params.scene_path
    if not full_scene_path.begins_with("res://"):
        full_scene_path = "res://" + full_scene_path
    
    if debug_mode:
        print("Full scene path (with res://): " + full_scene_path)
    
    # Ensure the output path starts with res:// for Godot's resource system
    var full_output_path = params.output_path
    if not full_output_path.begins_with("res://"):
        full_output_path = "res://" + full_output_path
    
    if debug_mode:
        print("Full output path (with res://): " + full_output_path)
    
    # Check if the scene file exists
    var file_check = FileAccess.file_exists(full_scene_path)
    if debug_mode:
        print("Scene file exists check: " + str(file_check))
    
    if not file_check:
        printerr("Scene file does not exist at: " + full_scene_path)
        # Get the absolute path for reference
        var absolute_path = ProjectSettings.globalize_path(full_scene_path)
        printerr("Absolute file path that doesn't exist: " + absolute_path)
        quit(1)
    
    # Load the scene
    if debug_mode:
        print("Loading scene from: " + full_scene_path)
    var scene = load(full_scene_path)
    if not scene:
        printerr("Failed to load scene: " + full_scene_path)
        quit(1)
    
    if debug_mode:
        print("Scene loaded successfully")
    
    # Instance the scene
    var scene_root = scene.instantiate()
    if debug_mode:
        print("Scene instantiated")
    
    # Create a new MeshLibrary
    var mesh_library = MeshLibrary.new()
    if debug_mode:
        print("Created new MeshLibrary")
    
    # Get mesh item names if provided
    var mesh_item_names = params.mesh_item_names if params.has("mesh_item_names") else []
    var use_specific_items = mesh_item_names.size() > 0
    
    if debug_mode:
        if use_specific_items:
            print("Using specific mesh items: " + str(mesh_item_names))
        else:
            print("Using all mesh items in the scene")
    
    # Process all child nodes
    var item_id = 0
    if debug_mode:
        print("Processing child nodes...")
    
    for child in scene_root.get_children():
        if debug_mode:
            print("Checking child node: " + child.name)
        
        # Skip if not using all items and this item is not in the list
        if use_specific_items and not (child.name in mesh_item_names):
            if debug_mode:
                print("Skipping node " + child.name + " (not in specified items list)")
            continue
            
        # Check if the child has a mesh
        var mesh_instance = null
        if child is MeshInstance3D:
            mesh_instance = child
            if debug_mode:
                print("Node " + child.name + " is a MeshInstance3D")
        else:
            # Try to find a MeshInstance3D in the child's descendants
            if debug_mode:
                print("Searching for MeshInstance3D in descendants of " + child.name)
            for descendant in child.get_children():
                if descendant is MeshInstance3D:
                    mesh_instance = descendant
                    if debug_mode:
                        print("Found MeshInstance3D in descendant: " + descendant.name)
                    break
        
        if mesh_instance and mesh_instance.mesh:
            if debug_mode:
                print("Adding mesh: " + child.name)
            
            # Add the mesh to the library
            mesh_library.create_item(item_id)
            mesh_library.set_item_name(item_id, child.name)
            mesh_library.set_item_mesh(item_id, mesh_instance.mesh)
            if debug_mode:
                print("Added mesh to library with ID: " + str(item_id))
            
            # Add collision shape if available
            var collision_added = false
            for collision_child in child.get_children():
                if collision_child is CollisionShape3D and collision_child.shape:
                    mesh_library.set_item_shapes(item_id, [collision_child.shape])
                    if debug_mode:
                        print("Added collision shape from: " + collision_child.name)
                    collision_added = true
                    break
            
            if debug_mode and not collision_added:
                print("No collision shape found for mesh: " + child.name)
            
            # Add preview if available
            if mesh_instance.mesh:
                mesh_library.set_item_preview(item_id, mesh_instance.mesh)
                if debug_mode:
                    print("Added preview for mesh: " + child.name)
            
            item_id += 1
        elif debug_mode:
            print("Node " + child.name + " has no valid mesh")
    
    if debug_mode:
        print("Processed " + str(item_id) + " meshes")
    
    # Create directory if it doesn't exist
    var dir = DirAccess.open("res://")
    if dir == null:
        printerr("Failed to open res:// directory")
        printerr("DirAccess error: " + str(DirAccess.get_open_error()))
        quit(1)
        
    var output_dir = full_output_path.get_base_dir()
    if debug_mode:
        print("Output directory: " + output_dir)
    
    if output_dir != "res://" and not dir.dir_exists(output_dir.substr(6)):  # Remove "res://" prefix
        if debug_mode:
            print("Creating directory: " + output_dir)
        var error = dir.make_dir_recursive(output_dir.substr(6))  # Remove "res://" prefix
        if error != OK:
            printerr("Failed to create directory: " + output_dir + ", error: " + str(error))
            quit(1)
    
    # Save the mesh library
    if item_id > 0:
        if debug_mode:
            print("Saving MeshLibrary to: " + full_output_path)
        var error = ResourceSaver.save(mesh_library, full_output_path)
        if debug_mode:
            print("Save result: " + str(error) + " (OK=" + str(OK) + ")")
        
        if error == OK:
            # Verify the file was actually created
            if debug_mode:
                var file_check_after = FileAccess.file_exists(full_output_path)
                print("File exists check after save: " + str(file_check_after))
                
                if file_check_after:
                    print("MeshLibrary exported successfully with " + str(item_id) + " items to: " + full_output_path)
                    # Get the absolute path for reference
                    var absolute_path = ProjectSettings.globalize_path(full_output_path)
                    print("Absolute file path: " + absolute_path)
                else:
                    printerr("File reported as saved but does not exist at: " + full_output_path)
            else:
                print("MeshLibrary exported successfully with " + str(item_id) + " items to: " + full_output_path)
        else:
            printerr("Failed to save MeshLibrary: " + str(error))
    else:
        printerr("No valid meshes found in the scene")

# Find files with a specific extension recursively
func find_files(path, extension):
    var files = []
    var dir = DirAccess.open(path)
    
    if dir:
        dir.list_dir_begin()
        var file_name = dir.get_next()
        
        while file_name != "":
            if dir.current_is_dir() and not file_name.begins_with("."):
                files.append_array(find_files(path + file_name + "/", extension))
            elif file_name.ends_with(extension):
                files.append(path + file_name)
            
            file_name = dir.get_next()
    
    return files

# Get UID for a specific file
func get_uid(params):
    if not params.has("file_path"):
        printerr("File path is required")
        quit(1)
    
    # Ensure the file path starts with res:// for Godot's resource system
    var file_path = params.file_path
    if not file_path.begins_with("res://"):
        file_path = "res://" + file_path
    
    print("Getting UID for file: " + file_path)
    if debug_mode:
        print("Full file path (with res://): " + file_path)
    
    # Get the absolute path for reference
    var absolute_path = ProjectSettings.globalize_path(file_path)
    if debug_mode:
        print("Absolute file path: " + absolute_path)
    
    # Ensure the file exists
    var file_check = FileAccess.file_exists(file_path)
    if debug_mode:
        print("File exists check: " + str(file_check))
    
    if not file_check:
        printerr("File does not exist at: " + file_path)
        printerr("Absolute file path that doesn't exist: " + absolute_path)
        quit(1)
    
    # Check if the UID file exists
    var uid_path = file_path + ".uid"
    if debug_mode:
        print("UID file path: " + uid_path)
    
    var uid_check = FileAccess.file_exists(uid_path)
    if debug_mode:
        print("UID file exists check: " + str(uid_check))
    
    var f = FileAccess.open(uid_path, FileAccess.READ)
    
    if f:
        # Read the UID content
        var uid_content = f.get_as_text()
        f.close()
        if debug_mode:
            print("UID content read successfully")
        
        # Return the UID content
        var result = {
            "file": file_path,
            "absolutePath": absolute_path,
            "uid": uid_content.strip_edges(),
            "exists": true
        }
        if debug_mode:
            print("UID result: " + JSON.stringify(result))
        print(JSON.stringify(result))
    else:
        if debug_mode:
            print("UID file does not exist or could not be opened")
        
        # UID file doesn't exist
        var result = {
            "file": file_path,
            "absolutePath": absolute_path,
            "exists": false,
            "message": "UID file does not exist for this file. Use resave_resources to generate UIDs."
        }
        if debug_mode:
            print("UID result: " + JSON.stringify(result))
        print(JSON.stringify(result))

# Resave all resources to update UID references
func resave_resources(params):
    print("Resaving all resources to update UID references...")
    
    # Get project path if provided
    var project_path = "res://"
    if params.has("project_path"):
        project_path = params.project_path
        if not project_path.begins_with("res://"):
            project_path = "res://" + project_path
        if not project_path.ends_with("/"):
            project_path += "/"
    
    if debug_mode:
        print("Using project path: " + project_path)
    
    # Get all .tscn files
    if debug_mode:
        print("Searching for scene files in: " + project_path)
    var scenes = find_files(project_path, ".tscn")
    if debug_mode:
        print("Found " + str(scenes.size()) + " scenes")
    
    # Resave each scene
    var success_count = 0
    var error_count = 0
    
    for scene_path in scenes:
        if debug_mode:
            print("Processing scene: " + scene_path)
        
        # Check if the scene file exists
        var file_check = FileAccess.file_exists(scene_path)
        if debug_mode:
            print("Scene file exists check: " + str(file_check))
        
        if not file_check:
            printerr("Scene file does not exist at: " + scene_path)
            error_count += 1
            continue
        
        # Load the scene
        var scene = load(scene_path)
        if scene:
            if debug_mode:
                print("Scene loaded successfully, saving...")
            var error = ResourceSaver.save(scene, scene_path)
            if debug_mode:
                print("Save result: " + str(error) + " (OK=" + str(OK) + ")")
            
            if error == OK:
                success_count += 1
                if debug_mode:
                    print("Scene saved successfully: " + scene_path)
                
                    # Verify the file was actually updated
                    var file_check_after = FileAccess.file_exists(scene_path)
                    print("File exists check after save: " + str(file_check_after))
                
                    if not file_check_after:
                        printerr("File reported as saved but does not exist at: " + scene_path)
            else:
                error_count += 1
                printerr("Failed to save: " + scene_path + ", error: " + str(error))
        else:
            error_count += 1
            printerr("Failed to load: " + scene_path)
    
    # Get all .gd and .shader files
    if debug_mode:
        print("Searching for script and shader files in: " + project_path)
    var scripts = find_files(project_path, ".gd") + find_files(project_path, ".shader") + find_files(project_path, ".gdshader")
    if debug_mode:
        print("Found " + str(scripts.size()) + " scripts/shaders")
    
    # Check for missing .uid files
    var missing_uids = 0
    var generated_uids = 0
    
    for script_path in scripts:
        if debug_mode:
            print("Checking UID for: " + script_path)
        var uid_path = script_path + ".uid"
        
        var uid_check = FileAccess.file_exists(uid_path)
        if debug_mode:
            print("UID file exists check: " + str(uid_check))
        
        var f = FileAccess.open(uid_path, FileAccess.READ)
        if not f:
            missing_uids += 1
            if debug_mode:
                print("Missing UID file for: " + script_path + ", generating...")
            
            # Force a save to generate UID
            var res = load(script_path)
            if res:
                var error = ResourceSaver.save(res, script_path)
                if debug_mode:
                    print("Save result: " + str(error) + " (OK=" + str(OK) + ")")
                
                if error == OK:
                    generated_uids += 1
                    if debug_mode:
                        print("Generated UID for: " + script_path)
                    
                        # Verify the UID file was actually created
                        var uid_check_after = FileAccess.file_exists(uid_path)
                        print("UID file exists check after save: " + str(uid_check_after))
                    
                        if not uid_check_after:
                            printerr("UID file reported as generated but does not exist at: " + uid_path)
                else:
                    printerr("Failed to generate UID for: " + script_path + ", error: " + str(error))
            else:
                printerr("Failed to load resource: " + script_path)
        elif debug_mode:
            print("UID file already exists for: " + script_path)
    
    if debug_mode:
        print("Summary:")
        print("- Scenes processed: " + str(scenes.size()))
        print("- Scenes successfully saved: " + str(success_count))
        print("- Scenes with errors: " + str(error_count))
        print("- Scripts/shaders missing UIDs: " + str(missing_uids))
        print("- UIDs successfully generated: " + str(generated_uids))
    print("Resave operation complete")

# Save changes to a scene file
func save_scene(params):
    print("Saving scene: " + params.scene_path)
    
    # Ensure the scene path starts with res:// for Godot's resource system
    var full_scene_path = params.scene_path
    if not full_scene_path.begins_with("res://"):
        full_scene_path = "res://" + full_scene_path
    
    if debug_mode:
        print("Full scene path (with res://): " + full_scene_path)
    
    # Check if the scene file exists
    var file_check = FileAccess.file_exists(full_scene_path)
    if debug_mode:
        print("Scene file exists check: " + str(file_check))
    
    if not file_check:
        printerr("Scene file does not exist at: " + full_scene_path)
        # Get the absolute path for reference
        var absolute_path = ProjectSettings.globalize_path(full_scene_path)
        printerr("Absolute file path that doesn't exist: " + absolute_path)
        quit(1)
    
    # Load the scene
    var scene = load(full_scene_path)
    if not scene:
        printerr("Failed to load scene: " + full_scene_path)
        quit(1)
    
    if debug_mode:
        print("Scene loaded successfully")
    
    # Instance the scene
    var scene_root = scene.instantiate()
    if debug_mode:
        print("Scene instantiated")
    
    # Determine save path
    var save_path = params.new_path if params.has("new_path") else full_scene_path
    if params.has("new_path") and not save_path.begins_with("res://"):
        save_path = "res://" + save_path
    
    if debug_mode:
        print("Save path: " + save_path)
    
    # Create directory if it doesn't exist
    if params.has("new_path"):
        var dir = DirAccess.open("res://")
        if dir == null:
            printerr("Failed to open res:// directory")
            printerr("DirAccess error: " + str(DirAccess.get_open_error()))
            quit(1)
            
        var scene_dir = save_path.get_base_dir()
        if debug_mode:
            print("Scene directory: " + scene_dir)
        
        if scene_dir != "res://" and not dir.dir_exists(scene_dir.substr(6)):  # Remove "res://" prefix
            if debug_mode:
                print("Creating directory: " + scene_dir)
            var error = dir.make_dir_recursive(scene_dir.substr(6))  # Remove "res://" prefix
            if error != OK:
                printerr("Failed to create directory: " + scene_dir + ", error: " + str(error))
                quit(1)
    
    # Create a packed scene
    var packed_scene = PackedScene.new()
    var result = packed_scene.pack(scene_root)
    if debug_mode:
        print("Pack result: " + str(result) + " (OK=" + str(OK) + ")")
    
    if result == OK:
        if debug_mode:
            print("Saving scene to: " + save_path)
        var error = ResourceSaver.save(packed_scene, save_path)
        if debug_mode:
            print("Save result: " + str(error) + " (OK=" + str(OK) + ")")
        
        if error == OK:
            # Verify the file was actually created/updated
            if debug_mode:
                var file_check_after = FileAccess.file_exists(save_path)
                print("File exists check after save: " + str(file_check_after))
                
                if file_check_after:
                    print("Scene saved successfully to: " + save_path)
                    # Get the absolute path for reference
                    var absolute_path = ProjectSettings.globalize_path(save_path)
                    print("Absolute file path: " + absolute_path)
                else:
                    printerr("File reported as saved but does not exist at: " + save_path)
            else:
                print("Scene saved successfully to: " + save_path)
        else:
            printerr("Failed to save scene: " + str(error))
    else:
        printerr("Failed to pack scene: " + str(result))

# Load a scene file and return its instantiated root node
func load_scene_root(scene_path):
    var full_scene_path = scene_path
    if not full_scene_path.begins_with("res://"):
        full_scene_path = "res://" + full_scene_path

    if not FileAccess.file_exists(full_scene_path):
        printerr("Failed to find scene file at: " + full_scene_path)
        quit(1)
        return

    var scene = load(full_scene_path)
    if not scene:
        printerr("Failed to load scene: " + full_scene_path)
        quit(1)
        return

    return scene.instantiate()

# Pack and save a scene root back to its scene file
func save_scene_root(scene_root, scene_path):
    var full_scene_path = scene_path
    if not full_scene_path.begins_with("res://"):
        full_scene_path = "res://" + full_scene_path

    var packed_scene = PackedScene.new()
    var result = packed_scene.pack(scene_root)
    if result != OK:
        printerr("Failed to pack scene: " + str(result))
        quit(1)
        return

    var save_error = ResourceSaver.save(packed_scene, full_scene_path)
    if save_error != OK:
        printerr("Failed to save scene: " + str(save_error))
        quit(1)
        return

# Get a node inside an instantiated scene, accepting "root" or "root/..." paths
func get_node_in_scene(scene_root, node_path):
    var path = str(node_path)
    if path == "root" or path == "":
        return scene_root
    if path.begins_with("root/"):
        path = path.substr(5)  # Remove "root/" prefix
    return scene_root.get_node_or_null(path)

# Convert a JSON array [x,y,z] or dictionary {x,y,z} to a Vector3
func to_vector3(value) -> Variant:
    if value is Dictionary:
        return Vector3(float(value.get("x", 0.0)), float(value.get("y", 0.0)), float(value.get("z", 0.0)))
    if value is Array and value.size() >= 3:
        return Vector3(float(value[0]), float(value[1]), float(value[2]))
    return null

# Convert a JSON array [r,g,b] / [r,g,b,a] or dictionary {r,g,b,a} to a Color
func to_color(value) -> Variant:
    if value is Dictionary:
        return Color(float(value.get("r", 1.0)), float(value.get("g", 1.0)), float(value.get("b", 1.0)), float(value.get("a", 1.0)))
    if value is Array:
        if value.size() == 3:
            return Color(float(value[0]), float(value[1]), float(value[2]))
        if value.size() >= 4:
            return Color(float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    return null

# Create or update a light node in a scene
func set_lighting(params):
    print("Setting lighting in scene: " + params.scene_path)

    var scene_root = load_scene_root(params.scene_path)

    var light_type = params.light_type
    var node_name = params.node_name

    # Find an existing light with this name or create a new one
    var light = scene_root.find_child(node_name, true, false)
    if light == null:
        light = ClassDB.instantiate(light_type)
        if light == null or not (light is Light3D):
            printerr("Failed to instantiate light of type: " + light_type)
            quit(1)
            return
        light.name = node_name
        scene_root.add_child(light)
        light.owner = scene_root
        if debug_mode:
            print("Created new light node: " + node_name)
    elif not (light is Light3D):
        printerr("Failed to update lighting: existing node '" + node_name + "' is a " + light.get_class() + ", not a Light3D")
        quit(1)
        return
    elif debug_mode:
        print("Updating existing light node: " + node_name)

    # Apply transform if provided
    if params.has("transform"):
        var t = params.transform
        if t.has("position"):
            var pos = to_vector3(t.position)
            if pos != null:
                light.position = pos
        if t.has("rotation_degrees"):
            var rot = to_vector3(t.rotation_degrees)
            if rot != null:
                light.rotation_degrees = rot

    # Apply light properties if provided
    if params.has("properties"):
        var properties = params.properties
        if properties.has("color"):
            var color = to_color(properties.color)
            if color != null:
                light.light_color = color
        if properties.has("energy"):
            light.light_energy = float(properties.energy)
        if properties.has("shadow_enabled"):
            light.shadow_enabled = bool(properties.shadow_enabled)

    save_scene_root(scene_root, params.scene_path)
    print("Light '" + node_name + "' of type '" + light_type + "' configured successfully")

# Create or update a Camera3D in a scene, aimed at a target point
func place_camera(params):
    print("Placing camera in scene: " + params.scene_path)

    var scene_root = load_scene_root(params.scene_path)

    var node_name = params.node_name

    # Find an existing camera with this name or create a new one
    var camera = scene_root.find_child(node_name, true, false)
    if camera == null:
        camera = Camera3D.new()
        camera.name = node_name
        scene_root.add_child(camera)
        camera.owner = scene_root
        if debug_mode:
            print("Created new camera node: " + node_name)
    elif not (camera is Camera3D):
        printerr("Failed to place camera: existing node '" + node_name + "' is a " + camera.get_class() + ", not a Camera3D")
        quit(1)
        return

    var pos = to_vector3(params.position)
    var look_target = to_vector3(params.look_at)
    if pos == null or look_target == null:
        printerr("Failed to place camera: position and look_at must be {x,y,z} or [x,y,z]")
        quit(1)
        return

    var dir = look_target - pos
    if dir.length() < 0.0001:
        printerr("Failed to place camera: position and look_at are too close together")
        quit(1)
        return

    # Camera3D looks along its -Z axis; Basis.looking_at() builds a basis whose
    # -Z points at the target, without needing the node to be inside the tree.
    camera.transform = Transform3D(Basis.looking_at(dir.normalized()), pos)

    if params.has("fov"):
        camera.fov = float(params.fov)

    camera.current = true

    save_scene_root(scene_root, params.scene_path)
    print("Camera '" + node_name + "' placed successfully")

# Build a complete graybox previs set in one call: creates a fresh scene and
# populates it from a JSON node list ({type: box|capsule|sphere|plane|label|
# light, name, position, size, color?, text?}), then saves. Batch alternative
# to many add_node calls: one Godot process builds the whole set, so the
# per-call process spawn cost is paid once per set instead of once per node.
# The scene file is overwritten if it exists (idempotent rebuild).
func build_graybox_set(params):
    print("Building graybox set: " + params.scene_path)

    var nodes = params.get("nodes", [])
    if not (nodes is Array) or nodes.is_empty():
        printerr("Failed to build graybox set: nodes must be a non-empty array")
        quit(1)
        return

    var scene_root = Node3D.new()
    scene_root.name = "GrayboxSet"

    var counts = {}
    for spec in nodes:
        if not (spec is Dictionary) or not spec.has("type") or not spec.has("name"):
            printerr("Failed to build graybox set: each node needs 'type' and 'name'")
            quit(1)
            return
        var node = make_graybox_node(spec)
        if node == null:
            printerr("Failed to build graybox set: could not build node: " + str(spec))
            quit(1)
            return
        scene_root.add_child(node)
        node.owner = scene_root
        counts[spec.type] = counts.get(spec.type, 0) + 1

    # Optional default camera saved into the scene ({position, look_at, fov?})
    if params.has("camera") and params.camera is Dictionary:
        var cam_spec = params.camera
        var cam_pos = to_vector3(cam_spec.get("position"))
        var cam_target = to_vector3(cam_spec.get("look_at"))
        if cam_pos != null and cam_target != null and cam_pos.distance_to(cam_target) >= 0.0001:
            var camera = Camera3D.new()
            camera.name = "DefaultCamera"
            camera.transform = Transform3D(Basis.looking_at((cam_target - cam_pos).normalized()), cam_pos)
            camera.fov = float(cam_spec.get("fov", 75.0))
            camera.current = true
            scene_root.add_child(camera)
            camera.owner = scene_root

    save_scene_root(scene_root, params.scene_path)
    print("Graybox set built: " + str(nodes.size()) + " nodes " + JSON.stringify(counts))

# Create one node for build_graybox_set from its JSON spec.
# Returns null (after printing the reason) on an invalid spec.
func make_graybox_node(spec):
    var pos = to_vector3(spec.get("position", {"x": 0, "y": 0, "z": 0}))
    if pos == null:
        printerr("Invalid position in node spec: " + str(spec))
        return null
    var color = to_color(spec.get("color")) if spec.has("color") else null
    var node_type = str(spec.type)

    if node_type == "label":
        var label = Label3D.new()
        label.name = spec.name
        label.text = str(spec.get("text", spec.name))
        label.position = pos
        # Billboard so the name tag stays readable from any camera; generous
        # font size + outline keeps it legible against bright graybox props
        label.billboard = BaseMaterial3D.BILLBOARD_ENABLED
        label.font_size = int(spec.get("font_size", 48))
        label.outline_size = 12
        label.modulate = Color(1, 1, 1)
        label.outline_modulate = Color(0, 0, 0)
        return label

    if node_type == "light":
        var light_type = str(spec.get("light_type", "OmniLight3D"))
        var light = ClassDB.instantiate(light_type)
        if light == null or not (light is Light3D):
            printerr("Invalid light_type: " + light_type)
            return null
        light.name = spec.name
        light.position = pos
        if spec.has("rotation"):
            var rot = to_vector3(spec.rotation)
            if rot != null:
                light.rotation_degrees = rot
        if color != null:
            light.light_color = color
        light.light_energy = float(spec.get("energy", 1.0))
        light.shadow_enabled = bool(spec.get("shadow_enabled", true))
        return light

    # Mesh nodes: box / capsule / sphere / plane
    var mesh_instance = MeshInstance3D.new()
    mesh_instance.name = spec.name
    mesh_instance.position = pos
    var size = to_vector3(spec.get("size", {"x": 1, "y": 1, "z": 1}))
    if size == null:
        printerr("Invalid size in node spec: " + str(spec))
        return null

    match node_type:
        "box":
            var box_mesh = BoxMesh.new()
            box_mesh.size = size
            mesh_instance.mesh = box_mesh
        "plane":
            var plane_mesh = PlaneMesh.new()
            plane_mesh.size = Vector2(size.x, size.z)
            mesh_instance.mesh = plane_mesh
        "capsule":
            var capsule_mesh = CapsuleMesh.new()
            capsule_mesh.radius = size.x
            capsule_mesh.height = size.y
            mesh_instance.mesh = capsule_mesh
        "sphere":
            var sphere_mesh = SphereMesh.new()
            sphere_mesh.radius = size.x
            sphere_mesh.height = size.x * 2.0
            mesh_instance.mesh = sphere_mesh
        _:
            printerr("Unknown graybox node type: " + node_type)
            return null

    if color != null:
        var material = StandardMaterial3D.new()
        material.albedo_color = color
        mesh_instance.material_override = material
    return mesh_instance

# Make a Node3D continuously face another node at runtime.
# Note: Godot 4.3+ has LookAtModifier3D, but it is a SkeletonModifier3D and
# only works on bones under a Skeleton3D, so it cannot be attached to a plain
# Node3D. The simplest reliable implementation is a small runtime script that
# calls look_at() every frame, plus a one-shot alignment at edit time.
func attach_look_at_constraint(params):
    print("Attaching look-at constraint in scene: " + params.scene_path)

    var scene_root = load_scene_root(params.scene_path)

    var source = get_node_in_scene(scene_root, params.source_node)
    if source == null:
        printerr("Failed to attach look-at constraint: source node not found: " + str(params.source_node))
        quit(1)
        return

    var target = get_node_in_scene(scene_root, params.target_node)
    if target == null:
        printerr("Failed to attach look-at constraint: target node not found: " + str(params.target_node))
        quit(1)
        return

    if not (source is Node3D) or not (target is Node3D):
        printerr("Failed to attach look-at constraint: both nodes must be Node3D (or derived)")
        quit(1)
        return

    # Write the runtime script into the project (one file per source node)
    var script_path = "res://mcp_look_at_" + str(source.name).validate_filename() + ".gd"
    var script_content = "extends Node3D\n"
    script_content += "\n"
    script_content += "# Auto-generated by godot-mcp attach_look_at_constraint.\n"
    script_content += "# Rotates this node every frame so it faces the target node.\n"
    script_content += "\n"
    script_content += "@export var target_node_path: NodePath\n"
    script_content += "\n"
    script_content += "func _process(_delta):\n"
    script_content += "    var target = get_node_or_null(target_node_path)\n"
    script_content += "    if target and target is Node3D:\n"
    script_content += "        look_at(target.global_position)\n"

    var script_file = FileAccess.open(script_path, FileAccess.WRITE)
    if script_file == null:
        printerr("Failed to write look-at script: " + script_path + " (error " + str(FileAccess.get_open_error()) + ")")
        quit(1)
        return
    script_file.store_string(script_content)
    script_file.close()

    var script = load(script_path)
    if script == null:
        printerr("Failed to load look-at script: " + script_path)
        quit(1)
        return

    source.set_script(script)
    source.set("target_node_path", source.get_path_to(target))

    # Align once at edit time so the saved scene already faces the target
    var dir = target.global_position - source.global_position
    if dir.length() >= 0.0001:
        var basis = Basis.looking_at(dir.normalized())
        source.global_transform = Transform3D(basis, source.global_position)

    save_scene_root(scene_root, params.scene_path)
    print("Look-at constraint attached: '" + str(params.source_node) + "' now faces '" + str(params.target_node) + "' (script: " + script_path + ")")

# Get the AnimationPlayer node referenced by the operation params
func get_animation_player(scene_root, params):
    var player = get_node_in_scene(scene_root, params.player_node_path)
    if player == null:
        printerr("Failed to find AnimationPlayer node: " + str(params.player_node_path))
        quit(1)
        return
    if not (player is AnimationPlayer):
        printerr("Failed to use node as AnimationPlayer: " + str(params.player_node_path) + " is a " + player.get_class())
        quit(1)
        return
    return player

# Get an existing animation from the player's default AnimationLibrary ("")
func get_animation_or_fail(player, animation_name):
    var library = player.get_animation_library("")
    if library == null or not library.has_animation(animation_name):
        printerr("Failed to find animation '" + animation_name + "' (use create_animation first)")
        quit(1)
        return
    return library.get_animation(animation_name)

# Create a new animation in an AnimationPlayer's default AnimationLibrary ("")
func create_animation(params):
    print("Creating animation in scene: " + params.scene_path)

    var scene_root = load_scene_root(params.scene_path)
    var player = get_animation_player(scene_root, params)

    var library = player.get_animation_library("")
    if library == null:
        library = AnimationLibrary.new()
        player.add_animation_library("", library)

    var animation_name = params.animation_name
    var length = float(params.length) if params.has("length") else 1.0

    if library.has_animation(animation_name):
        # Idempotent: update the length of the existing animation
        library.get_animation(animation_name).length = length
        if debug_mode:
            print("Animation already exists, updated length: " + animation_name)
    else:
        var anim = Animation.new()
        anim.length = length
        var error = library.add_animation(animation_name, anim)
        if error != OK:
            printerr("Failed to add animation '" + animation_name + "': " + str(error))
            quit(1)
            return

    save_scene_root(scene_root, params.scene_path)
    print("Animation '" + animation_name + "' created successfully (length " + str(length) + "s)")

# Add a track to an animation
func add_track(params):
    print("Adding track to animation in scene: " + params.scene_path)

    var scene_root = load_scene_root(params.scene_path)
    var player = get_animation_player(scene_root, params)
    var anim = get_animation_or_fail(player, params.animation_name)

    var target = get_node_in_scene(scene_root, params.target_node_path)
    if target == null:
        printerr("Failed to add track: target node not found: " + str(params.target_node_path))
        quit(1)
        return

    var track_type = -1
    match params.track_type:
        "value":
            track_type = Animation.TYPE_VALUE
        "position_3d":
            track_type = Animation.TYPE_POSITION_3D
        "rotation_3d":
            track_type = Animation.TYPE_ROTATION_3D
        "scale_3d":
            track_type = Animation.TYPE_SCALE_3D
        _:
            printerr("Failed to add track: unknown track type: " + str(params.track_type))
            quit(1)
            return

    var track_index = anim.add_track(track_type)
    if track_index < 0:
        printerr("Failed to add track to animation '" + params.animation_name + "'")
        quit(1)
        return

    # Track paths are resolved relative to the AnimationPlayer's root_node
    var base = player.get_node_or_null(player.root_node)
    if base == null:
        base = scene_root
    var track_path = str(base.get_path_to(target))

    if track_type == Animation.TYPE_VALUE:
        if not params.has("property"):
            printerr("Failed to add track: value tracks require a 'property' parameter")
            quit(1)
            return
        track_path += ":" + str(params.property)

    anim.track_set_path(track_index, NodePath(track_path))

    # Optional batch keyframe insertion: one call fills the whole track.
    # Adapted from hi-godot/godot-ai plugin/addons/godot_ai/handlers/animation_handler.gd
    # (_do_add_property_track), which inserts keyframes in a single loop.
    var keyframe_count = 0
    if params.has("keyframes"):
        var keyframes = params.keyframes
        if not (keyframes is Array):
            printerr("Failed to add track: keyframes must be an array of {time, value, transition?}")
            quit(1)
            return
        for kf in keyframes:
            if not (kf is Dictionary) or not kf.has("time") or not kf.has("value"):
                printerr("Failed to add track: each keyframe must be a dictionary with 'time' and 'value'")
                quit(1)
                return
            var key_time = float(kf.time)
            var key_value = parse_key_value(kf.value)
            var key_transition = float(kf.get("transition", 1.0))
            var key_index = anim.track_insert_key(track_index, key_time, key_value, key_transition)
            if key_index < 0:
                printerr("Failed to add track: keyframe at " + str(key_time) + "s could not be inserted (value type may not match track type)")
                quit(1)
                return
            keyframe_count += 1

    save_scene_root(scene_root, params.scene_path)
    print(JSON.stringify({
        "animation": params.animation_name,
        "track_type": params.track_type,
        "track_index": track_index,
        "track_path": track_path,
        "keyframe_count": keyframe_count
    }))

# Convert a JSON key value to the matching Godot type.
# Arrays: [x,y] -> Vector2, [x,y,z] -> Vector3, [x,y,z,w] -> Quaternion.
# rotation_3d tracks therefore take quaternion keys ([x,y,z,w]); position_3d
# and scale_3d tracks take Vector3 keys ([x,y,z]).
func parse_key_value(value) -> Variant:
    if value is Array:
        if value.size() == 2:
            return Vector2(float(value[0]), float(value[1]))
        if value.size() == 3:
            return Vector3(float(value[0]), float(value[1]), float(value[2]))
        if value.size() == 4:
            return Quaternion(float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    return value

# Find the key index at (approximately) a given time, or -1 if none.
# Adapted from youichi-uda/godot-mcp-pro addons/godot_mcp/commands/animation_commands.gd
# (_find_animation_key_at_time).
func find_animation_key_at_time(anim, track_index, time):
    for key_index in anim.track_get_key_count(track_index):
        if is_equal_approx(anim.track_get_key_time(track_index, key_index), time):
            return key_index
    return -1

# Insert a keyframe into an animation track.
# Upsert semantics (adapted from youichi-uda/godot-mcp-pro
# addons/godot_mcp/commands/animation_commands.gd _upsert_animation_key):
# if a key already exists at the same time it is updated in place, otherwise
# a new key is inserted. Optional 'transition' (easing, default 1.0).
func insert_keyframe(params):
    print("Inserting keyframe in scene: " + params.scene_path)

    var scene_root = load_scene_root(params.scene_path)
    var player = get_animation_player(scene_root, params)
    var anim = get_animation_or_fail(player, params.animation_name)

    var track_index = int(params.track_index)
    if track_index < 0 or track_index >= anim.get_track_count():
        printerr("Failed to insert keyframe: track index " + str(track_index) + " out of range (" + str(anim.get_track_count()) + " tracks)")
        quit(1)
        return

    var time = float(params.time)
    var value = parse_key_value(params.value)
    var transition = float(params.get("transition", 1.0))

    var existing_key = find_animation_key_at_time(anim, track_index, time)
    var action = "inserted"
    var key_index = existing_key
    if existing_key >= 0:
        # Update the existing key in place (time is unchanged). There is no
        # track_update_key in the Godot 4 Animation API, so set value and
        # transition directly like godot-mcp-pro does.
        anim.track_set_key_value(track_index, existing_key, value)
        anim.track_set_key_transition(track_index, existing_key, transition)
        action = "updated"
    else:
        # track_insert_key returns the inserted key index, or -1 on failure
        # (it is not an Error code, so values > 0 are not errors)
        key_index = anim.track_insert_key(track_index, time, value, transition)
        if key_index < 0:
            printerr("Failed to insert keyframe: value type may not match track type")
            quit(1)
            return

    save_scene_root(scene_root, params.scene_path)
    print(JSON.stringify({
        "animation": params.animation_name,
        "track_index": track_index,
        "time": time,
        "key_index": key_index,
        "action": action,
        "key_count": anim.track_get_key_count(track_index)
    }))

# Set the length of an existing animation
func set_animation_length(params):
    print("Setting animation length in scene: " + params.scene_path)

    var scene_root = load_scene_root(params.scene_path)
    var player = get_animation_player(scene_root, params)
    var anim = get_animation_or_fail(player, params.animation_name)

    anim.length = float(params.length)

    save_scene_root(scene_root, params.scene_path)
    print("Animation '" + params.animation_name + "' length set to " + str(params.length) + "s")

# Parse a resolution param ({width, height} or [width, height]), defaulting to 1280x720
func parse_resolution(params):
    var width = 1280
    var height = 720
    if params.has("resolution"):
        var resolution = params.resolution
        if resolution is Dictionary:
            width = int(resolution.get("width", width))
            height = int(resolution.get("height", height))
        elif resolution is Array and resolution.size() >= 2:
            width = int(resolution[0])
            height = int(resolution[1])
    return Vector2i(width, height)

# Pick the camera: inline spec, explicit path, otherwise the first Camera3D in the scene
func find_render_camera(scene_root, params):
    var camera = null
    # Inline camera spec ({position, look_at, fov?}): build a throwaway camera
    # for this render only; the scene file is never modified or saved here.
    if params.has("camera") and params.camera is Dictionary:
        var spec = params.camera
        var pos = to_vector3(spec.get("position"))
        var look_target = to_vector3(spec.get("look_at"))
        if pos == null or look_target == null:
            printerr("Failed to render: inline camera needs position and look_at as {x,y,z}")
            quit(1)
            return
        var dir = look_target - pos
        if dir.length() < 0.0001:
            printerr("Failed to render: inline camera position and look_at are too close together")
            quit(1)
            return
        camera = Camera3D.new()
        camera.name = "InlineRenderCamera"
        # The camera must be parented before its global_transform is set: a
        # detached Node3D reports an identity global_transform, which would
        # leave the render camera at the origin. The scene is never saved in
        # the render ops, so this throwaway node has no side effects.
        scene_root.add_child(camera)
        camera.global_transform = Transform3D(Basis.looking_at(dir.normalized()), pos)
        camera.fov = float(spec.get("fov", 75.0))
        return camera
    if params.has("camera_node_path") and str(params.camera_node_path) != "":
        camera = get_node_in_scene(scene_root, params.camera_node_path)
        if camera == null or not (camera is Camera3D):
            printerr("Failed to render: camera not found or not a Camera3D: " + str(params.camera_node_path))
            quit(1)
            return
    else:
        # find_children supports a type filter (find_child does not)
        var cameras = scene_root.find_children("*", "Camera3D", true, false)
        if cameras.size() > 0:
            camera = cameras[0]

    if camera == null:
        printerr("Failed to render: no Camera3D found in scene (use place_camera first or pass cameraNodePath)")
        quit(1)
        return
    return camera

# Create a throwaway SubViewport rendering the shared world from a clone of
# the given camera. Adapted from hi-godot/godot-ai
# plugin/addons/godot_ai/handlers/editor_handler.gd _take_cinematic_screenshot:
# a SubViewport (own_world_3d=false) + cloned camera + force_update_transform()
# + RenderingServer.force_draw(false) captures a frame synchronously, without
# waiting for _process frames.
func make_render_viewport(scene_root, source_camera, size):
    var sub_vp = SubViewport.new()
    sub_vp.size = size
    sub_vp.own_world_3d = false
    sub_vp.transparent_bg = false
    sub_vp.render_target_update_mode = SubViewport.UPDATE_ONCE

    var cam = Camera3D.new()
    cam.fov = source_camera.fov
    cam.near = source_camera.near
    cam.far = source_camera.far
    cam.projection = source_camera.projection
    cam.size = source_camera.size
    cam.keep_aspect = source_camera.keep_aspect
    cam.cull_mask = source_camera.cull_mask
    cam.current = true

    sub_vp.add_child(cam)
    scene_root.add_child(sub_vp)
    # global_transform is resolved against the ancestor chain, so it must be
    # set after parenting; force_update_transform() flushes it to the
    # RenderingServer immediately (deferred transform notifications would
    # otherwise leave the camera at the origin for the forced draw).
    cam.global_transform = source_camera.global_transform
    cam.force_update_transform()

    if debug_mode:
        print("render viewport size: ", sub_vp.size)
        print("source camera transform: ", source_camera.global_transform)
        print("render camera transform: ", cam.global_transform)
        print("render camera fov: ", cam.fov)

    return sub_vp

# Resolve an output path and make sure its directory exists
func prepare_output_path(output_path):
    if not output_path.begins_with("res://"):
        output_path = "res://" + output_path
    var absolute_output_path = ProjectSettings.globalize_path(output_path)

    var make_dir_error = DirAccess.make_dir_recursive_absolute(absolute_output_path.get_base_dir())
    if make_dir_error != OK:
        printerr("Failed to create output directory: " + str(make_dir_error))
        quit(1)
        return

    return absolute_output_path

# Save a viewport's current contents to a PNG file
func save_viewport_png(viewport, absolute_output_path):
    var texture = viewport.get_texture()
    if texture == null:
        printerr("Failed to render: viewport has no texture")
        quit(1)
        return

    var image = texture.get_image()
    if image == null or image.is_empty():
        printerr("Failed to render: captured image is empty")
        quit(1)
        return

    var save_error = image.save_png(absolute_output_path)
    if save_error != OK:
        printerr("Failed to save PNG: " + str(save_error))
        quit(1)
        return

    return image

# Render one frame of a scene from its current (or specified) camera to a PNG.
# Must run windowed (no --headless) since headless uses the dummy rasterizer.
# SubViewport capture adapted from hi-godot/godot-ai
# plugin/addons/godot_ai/handlers/editor_handler.gd _take_cinematic_screenshot
# (see make_render_viewport). Two-phase: this only sets up the scene and the
# throwaway SubViewport; the actual capture happens in finish_render_capture
# a few frames later, because a SubViewport + camera created and force-drawn
# in the same frame renders with stale (identity) camera transforms.
# Render many captures of ONE scene inside a single process.
#
# params: {scene_path, resolution?, shots: [{shot_id, camera, outputs:{rgb?,depth?,normal?}}]}
#
# Why this exists: see the batch_jobs declaration. The per-capture semantics are
# identical to render_viewport / capture_aux_buffer — same throwaway SubViewport,
# same two-phase deferred capture, same material_override trick for depth/normal
# — the only difference is that the scene is loaded once and the process is torn
# down once. Labels are hidden for the whole batch: batch output is model
# conditioning, never a human-facing previs frame.
# Apply the depth/normal material override + matching background to a set of
# meshes and a render camera. Single implementation shared by capture_aux_buffer
# (one capture per process) and render_shot_batch (many captures per process),
# so the two paths can never drift apart.
func apply_aux_material(meshes, render_cam, buffer_type):
    var shader = Shader.new()
    if buffer_type == "normal":
        shader.code = """
shader_type spatial;
render_mode unshaded, cull_disabled, fog_disabled;

void fragment() {
    ALBEDO = normalize(NORMAL) * 0.5 + 0.5;
}
"""
    else:
        shader.code = """
shader_type spatial;
render_mode unshaded, cull_disabled, fog_disabled;

uniform float far_plane = 100.0;
varying float view_depth;

void vertex() {
    view_depth = -(MODELVIEW_MATRIX * vec4(VERTEX, 1.0)).z;
}

void fragment() {
    float gray = clamp(view_depth / far_plane, 0.0, 1.0);
    ALBEDO = vec3(gray);
}
"""
    var material = ShaderMaterial.new()
    material.shader = shader
    for mesh in meshes:
        mesh.material_override = material

    var env = Environment.new()
    env.background_mode = Environment.BG_COLOR
    if buffer_type == "normal":
        # Flat mid-gray = zero normal, so empty regions carry no fake direction.
        env.background_color = Color(0.5, 0.5, 0.5)
        render_cam.environment = env
        return

    # Fit the normalization range to the scene so the 8-bit grayscale keeps
    # precision: normalize by the distance from the camera to the farthest
    # scene AABB corner instead of the (usually huge) camera far plane.
    var depth_range = render_cam.far
    var have_aabb = false
    var scene_aabb = AABB()
    for mesh in meshes:
        var mesh_aabb = mesh.global_transform * mesh.get_aabb()
        if not have_aabb:
            scene_aabb = mesh_aabb
            have_aabb = true
        else:
            scene_aabb = scene_aabb.merge(mesh_aabb)
    if have_aabb:
        var cam_pos = render_cam.global_transform.origin
        var max_dist = 0.0
        for cx in [scene_aabb.position.x, scene_aabb.end.x]:
            for cy in [scene_aabb.position.y, scene_aabb.end.y]:
                for cz in [scene_aabb.position.z, scene_aabb.end.z]:
                    max_dist = max(max_dist, cam_pos.distance_to(Vector3(cx, cy, cz)))
        if max_dist > 0.001:
            depth_range = max_dist
    material.set_shader_parameter("far_plane", depth_range)

    # Sky/background counts as farthest (white) in the depth map
    env.background_color = Color(1, 1, 1)
    render_cam.environment = env


func render_shot_batch(params):
    if not params.has("shots") or not (params.shots is Array) or params.shots.is_empty():
        printerr("Failed to render batch: 'shots' must be a non-empty array")
        quit(1)
        return

    batch_scene_root = load_scene_root(params.scene_path)
    root.add_child(batch_scene_root)
    batch_meshes = batch_scene_root.find_children("*", "MeshInstance3D", true, false)
    for label in batch_scene_root.find_children("*", "Label3D", true, false):
        label.visible = false

    var size = parse_resolution(params)
    for shot in params.shots:
        if not shot.has("outputs"):
            continue
        for chan in ["rgb", "depth", "normal"]:
            if shot.outputs.has(chan) and str(shot.outputs[chan]) != "":
                batch_jobs.append({
                    "shot_id": str(shot.get("shot_id", "")),
                    "channel": chan,
                    "camera": shot.get("camera"),
                    "output": str(shot.outputs[chan]),
                    "size": size,
                })

    print("Batch: " + str(batch_jobs.size()) + " captures from " + params.scene_path)
    start_next_batch_job()


func start_next_batch_job():
    if batch_jobs.is_empty():
        print(JSON.stringify({"captures": batch_results.size(), "outputs": batch_results}))
        quit()
        return

    var job = batch_jobs.pop_front()
    var camera = find_render_camera(batch_scene_root, {"camera": job.camera})
    if camera == null:
        printerr("Failed to render batch: bad camera for " + job.shot_id)
        quit(1)
        return
    var absolute_output_path = prepare_output_path(job.output)
    var sub_vp = make_render_viewport(batch_scene_root, camera, job.size)
    var render_cam = sub_vp.get_child(0)

    if job.channel == "rgb":
        # Clear any override left behind by a previous depth/normal job —
        # the scene object is reused across the whole batch.
        for mesh in batch_meshes:
            mesh.material_override = null
        pending_capture = ["render", sub_vp, absolute_output_path, batch_scene_root]
    else:
        pending_buffer_type = job.channel
        apply_aux_material(batch_meshes, render_cam, job.channel)
        pending_capture = ["aux", sub_vp, absolute_output_path, batch_scene_root]

    # The throwaway inline camera built by find_render_camera is parented to the
    # scene root and would otherwise pile up across the batch.
    if camera.name == "InlineRenderCamera":
        camera.queue_free()
    capture_wait_frames = 0


func render_viewport(params):
    print("Rendering viewport for scene: " + params.scene_path)

    var scene_root = load_scene_root(params.scene_path)
    root.add_child(scene_root)

    var size = parse_resolution(params)
    var camera = find_render_camera(scene_root, params)
    var absolute_output_path = prepare_output_path(params.output_path)

    # The graybox annotation labels (character/prop names) are useful when a
    # human reviews the previs frame, but they are pure noise when the frame is
    # fed to a structure-conditioned model — a canny map derived from such a
    # frame turns the lettering into strong fake edges. Callers producing
    # ControlNet conditioning pass hide_labels; human-facing previs does not.
    if params.get("hide_labels", false):
        for label in scene_root.find_children("*", "Label3D", true, false):
            label.visible = false

    var sub_vp = make_render_viewport(scene_root, camera, size)

    if debug_mode:
        print("Rendering from camera: " + camera.name + " at " + str(size.x) + "x" + str(size.y))

    pending_capture = ["render", sub_vp, absolute_output_path, scene_root]

# Second phase of render_viewport / capture_aux_buffer, called from _process
# a few frames after setup (see render_viewport for why two phases).
func finish_render_capture():
    var kind = pending_capture[0]
    var sub_vp = pending_capture[1]
    var absolute_output_path = pending_capture[2]
    var scene_root = pending_capture[3]
    pending_capture = null

    RenderingServer.force_draw(false)

    if kind == "render":
        var image = save_viewport_png(sub_vp, absolute_output_path)
        if batch_scene_root == null:
            print(JSON.stringify({
                "output": absolute_output_path,
                "width": image.get_width(),
                "height": image.get_height()
            }))
    else:
        finish_aux_capture(sub_vp, absolute_output_path, pending_buffer_type)

    scene_root.remove_child(sub_vp)
    sub_vp.queue_free()

    # In batch mode the scene stays loaded and we roll straight into the next
    # capture instead of tearing the process down.
    if batch_scene_root != null:
        batch_results.append({"output": absolute_output_path})
        start_next_batch_job()
        return
    quit()

# Capture an auxiliary render buffer (currently only "depth") of a scene from
# its camera to a PNG. Must run windowed (no --headless). Called from _process
# on the first frame, when the scene tree is active.
#
# Depth approach: instead of paperman5/godot-depth-buffer-plugin's
# CompositorEffect compute shader (which needs a .glsl imported into the user
# project plus render-thread plumbing), the scene is rendered once into a
# throwaway SubViewport with every mesh's material overridden by an unshaded
# shader that writes view-space depth (normalized by the camera far plane)
# directly as color. A hint_depth_texture full-screen quad was tried first,
# but in this one-shot force_draw capture the quad viewport sampled its own
# depth instead of the source viewport's (flat output). The material override
# needs no depth-texture readback at all and works on any renderer; the scene
# instance is discarded unsaved, so the override has no side effects.
# The result is auto-stretched to full grayscale (near = dark, far = bright),
# since scene depths usually occupy a tiny fraction of [near, far].
func capture_aux_buffer(params):
    print("Capturing auxiliary buffer for scene: " + params.scene_path)

    var buffer_type = params.get("buffer_type", "depth")
    if buffer_type != "depth" and buffer_type != "normal":
        printerr("Failed to capture auxiliary buffer: unsupported buffer type: " + str(buffer_type) + " (supported: 'depth', 'normal')")
        quit(1)
        return
    pending_buffer_type = buffer_type

    var scene_root = load_scene_root(params.scene_path)
    root.add_child(scene_root)

    var size = parse_resolution(params)
    var camera = find_render_camera(scene_root, params)
    var absolute_output_path = prepare_output_path(params.output_path)

    var sub_vp = make_render_viewport(scene_root, camera, size)
    var render_cam = sub_vp.get_child(0)

    # Unshaded shader writing the requested buffer straight out as color.
    # depth : view-space depth, normalized by the scene-fitted range below.
    # normal: view-space normal encoded n*0.5+0.5, the convention ControlNet
    #         normal models expect. In fragment() NORMAL is already view-space,
    #         so no manual matrix work (and no vertex-stage varying) is needed.
    var meshes = scene_root.find_children("*", "MeshInstance3D", true, false)
    # Annotation labels (graybox set/role names) are Label3D, not
    # MeshInstance3D, so material_override never reaches them and they would
    # render as solid text into the buffer — spurious structure for any
    # downstream ControlNet. The scene instance is discarded unsaved.
    for label in scene_root.find_children("*", "Label3D", true, false):
        label.visible = false
    apply_aux_material(meshes, render_cam, buffer_type)

    # Capture deferred like render_viewport (same stale-transform pitfall)
    pending_capture = ["aux", sub_vp, absolute_output_path, scene_root]

# Second phase of capture_aux_buffer: runs in finish_render_capture after the
# viewport had a few frames to settle; does the depth auto-stretch and save.
func finish_aux_capture(sub_vp, absolute_output_path, buffer_type = "depth"):
    var texture = sub_vp.get_texture()
    if texture == null:
        printerr("Failed to capture auxiliary buffer: viewport has no texture")
        quit(1)
        return
    var image = texture.get_image()
    if image == null or image.is_empty():
        printerr("Failed to capture auxiliary buffer: captured image is empty")
        quit(1)
        return

    # Normals are already in their final encoding; stretching would destroy the
    # n*0.5+0.5 mapping. Save as-is.
    if buffer_type == "normal":
        var normal_err = image.save_png(absolute_output_path)
        if normal_err != OK:
            printerr("Failed to save PNG: " + str(normal_err))
            quit(1)
            return
        if batch_scene_root == null:
            print(JSON.stringify({
                "output": absolute_output_path,
                "width": image.get_width(),
                "height": image.get_height(),
                "buffer_type": "normal"
            }))
        return

    # Auto-stretch the observed depth range to full grayscale. Sky pixels
    # (rendered white, ~1.0) are excluded from the range computation and
    # pinned to white so they stay the farthest value.
    var min_v = 1.0
    var max_v = 0.0
    for y in image.get_height():
        for x in image.get_width():
            var v = image.get_pixel(x, y).r
            if v >= 0.999:
                continue
            if v < min_v:
                min_v = v
            if v > max_v:
                max_v = v
    if max_v - min_v > 0.00001:
        for y in image.get_height():
            for x in image.get_width():
                var c = image.get_pixel(x, y)
                var n = 1.0
                if c.r < 0.999:
                    n = (c.r - min_v) / (max_v - min_v)
                image.set_pixel(x, y, Color(n, n, n, 1.0))

    var save_error = image.save_png(absolute_output_path)
    if save_error != OK:
        printerr("Failed to save PNG: " + str(save_error))
        quit(1)
        return

    if batch_scene_root == null:
        print(JSON.stringify({
            "output": absolute_output_path,
            "width": image.get_width(),
            "height": image.get_height(),
            "buffer_type": "depth"
        }))
