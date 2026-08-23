# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import bpy

from . import gamepad

from bpy.types import (Operator, Panel, AddonPreferences)

#--------------------------------------------------------------------------------------------------------------------------------#
#----------------------------------------------------------PREFERENCES-----------------------------------------------------------#
#--------------------------------------------------------------------------------------------------------------------------------#

class XR_PT_preferences_panel(AddonPreferences):
    bl_idname = __package__

    controller_index: bpy.props.IntProperty(
        name="Controller",
        description="Which of the connected controllers to read, 0 is the first one",
        default=0,
        min=0,
        max=3,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "controller_index")

        box = layout.box()
        row = box.row()
        row.label(text="Detected Controllers")
        row.operator("wm.refresh_controllers", text="", icon='FILE_REFRESH')

        try:
            devices = get_devices()
        except gamepad.GamepadError as error:
            box.label(text=str(error), icon='ERROR')
            return

        if not devices:
            box.label(text="No controller found", icon='INFO')
            return

        for index, name in enumerate(devices):
            box.label(text="{}: {}".format(index, name), icon='PLAY')

#------------------------------------------------------------------------------------------------------------------------------#
#----------------------------------------------------------FUNCTIONS-----------------------------------------------------------#
#------------------------------------------------------------------------------------------------------------------------------#


# Scanning for devices means opening every input device, so the result is
# cached until the refresh button in the preferences is used.
_devices = None


def get_devices():
    global _devices
    if _devices is None:
        _devices = gamepad.list_devices()
    return _devices


def refresh_devices():
    global _devices
    _devices = None


def rigid_body_cache(scene):
    """The point cache of the scene's rigid body world, if it has one."""
    world = scene.rigidbody_world
    return world.point_cache if world else None


def reset_rigid_body(context):
    """Throw away the simulated frames, returns False without a rigid body world.

    Blender does not notice when a constraint is changed by a driver, so the
    simulation is replayed from the cache exactly as it was and looks stuck on
    the previous take. Emptying the point cache does not help by itself:
    writing one of the world's own settings, even the very same value back, is
    what actually makes it simulate again.
    """
    world = context.scene.rigidbody_world
    if world is None:
        return False

    with context.temp_override(point_cache=world.point_cache):
        bpy.ops.ptcache.free_bake()

    world.substeps_per_frame = world.substeps_per_frame
    return True


def get_preferences(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def get_reader():
    return bpy.data.objects.get("XInput Reader")


# The keyframes of a recording end up in this action group, and every
# recorded property is animated by a "[\"name\"]" f-curve.
RECORDING_GROUP = "XInput Reader"

RECORDED_PATHS = {'["{}"]'.format(name): name for name in gamepad.INPUT_NAMES}


def reader_fcurves(reader):
    """The reader's f-curves, on both the old and the new action API."""
    animation_data = reader.animation_data
    action = animation_data.action if animation_data else None

    if action is None:
        return []

    # Blender 4.4 and later keep the curves in the slot's channel bag
    if getattr(action, "layers", None):
        slot = animation_data.action_slot
        for layer in action.layers:
            for strip in layer.strips:
                channelbag = strip.channelbag(slot) if slot else None
                if channelbag is not None:
                    return channelbag.fcurves

    return getattr(action, "fcurves", [])


def get_recording(reader):
    """The recorded f-curves of the reader, ignoring anything else on it."""
    return [fcurve for fcurve in reader_fcurves(reader)
            if fcurve.data_path in RECORDED_PATHS]


def get_recorded_range(reader):
    """The first and last recorded frame, or None when there is no recording."""
    frames = [point.co[0] for fcurve in get_recording(reader)
              for point in fcurve.keyframe_points]
    if not frames:
        return None
    return int(min(frames)), int(max(frames))


def insert_keyframes(reader, frame):
    for name in gamepad.INPUT_NAMES:
        reader.keyframe_insert(data_path='["{}"]'.format(name), frame=frame,
                               group=RECORDING_GROUP)


def apply_recorded_interpolation(reader):
    """Buttons switch from one frame to the next, sticks and triggers ramp."""
    for fcurve in reader_fcurves(reader):
        name = RECORDED_PATHS.get(fcurve.data_path)
        if name is None:
            continue

        interpolation = 'CONSTANT' if name in gamepad.BUTTON_NAMES else 'LINEAR'
        for point in fcurve.keyframe_points:
            point.interpolation = interpolation
        fcurve.update()


def clear_recording(reader):
    """Remove the recorded curves, leaving any other animation alone."""
    recorded = get_recording(reader)
    fcurves = reader_fcurves(reader)

    if len(recorded) == len(fcurves):
        reader.animation_data_clear()
        return

    for fcurve in recorded:
        fcurves.remove(fcurve)

def create_reader():
    xinput_reader_empty = bpy.data.objects.get("XInput Reader")
    if xinput_reader_empty is None:
        xinput_reader_empty = bpy.data.objects.new("XInput Reader", None)
        xinput_reader_empty.use_fake_user = True
        # bpy.context.scene.collection.objects.link(xinput_reader_empty)
    return xinput_reader_empty



#------------------------------------------------------------------------------------------------------------------------------#
#----------------------------------------------------------OPERATORS-----------------------------------------------------------#
#------------------------------------------------------------------------------------------------------------------------------#


class XR_OT_monitor_controller(Operator):
    bl_idname = "wm.monitor_controller"
    bl_label = "Monitor Controller"
    bl_description = "Monitors controller input"
    bl_options = {'REGISTER'}

    record: bpy.props.BoolProperty(
        name="Record",
        description="Play the animation and key the controller inputs as it goes",
        default=False,
        options={'SKIP_SAVE'},
    )
    from_start: bpy.props.BoolProperty(
        name="Start From First Frame",
        description="Jump back to the first frame before recording, so that "
                    "simulations are run from their beginning",
        default=True,
        options={'SKIP_SAVE'},
    )
    clear_previous: bpy.props.BoolProperty(
        name="Clear Previous Recording",
        description="Throw away an earlier recording instead of recording over "
                    "the part of it that is played through",
        default=True,
        options={'SKIP_SAVE'},
    )

    _timer = None
    _gamepad = None
    _first_frame = None
    _last_frame = None
    _was_playing = False

    def modal(self, context, event):
        xinput_reader_empty = get_reader()

        if xinput_reader_empty is None or event.type in {'RIGHTMOUSE', 'ESC'}:
            self.cancel(context)
            if xinput_reader_empty is not None:
                xinput_reader_empty.location = xinput_reader_empty.location
            return {'CANCELLED'}

        #Controller inputs
        values = self._gamepad.poll()

        if values is not None:  # None means nothing is plugged in
            for name, value in values.items():
                xinput_reader_empty[name] = value

            # trigger scene update
            xinput_reader_empty.location = xinput_reader_empty.location

        if self.record and self.record_frames(context, xinput_reader_empty):
            self.cancel(context)
            return {'FINISHED'}

        return {'PASS_THROUGH'}

    def record_frames(self, context, xinput_reader_empty):
        """Key the current values, returns True once the recording is done."""
        scene = context.scene
        frame = scene.frame_current

        playing = context.screen.is_animation_playing if context.screen else False
        self._was_playing = self._was_playing or playing

        if frame != self._last_frame:
            if self._last_frame is not None and frame < self._last_frame:
                return True  # the playback looped back around

            insert_keyframes(xinput_reader_empty, frame)

            if self._first_frame is None:
                self._first_frame = frame
            self._last_frame = frame

        if frame >= scene.frame_end:
            return True

        # the user stopped the playback themselves
        return self._was_playing and not playing

    def execute(self, context):

        preferences = get_preferences(context)
        controller_index = preferences.controller_index if preferences else 0

        try:
            self._gamepad = gamepad.open_gamepad(controller_index)
        except gamepad.GamepadError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}

        self.report({'INFO'}, "Reading {}".format(self._gamepad.describe()))

        xinput_reader_empty = create_reader()

        self._first_frame = None
        self._last_frame = None
        self._was_playing = False

        if self.record:
            self.start_recording(context, xinput_reader_empty)

        wm = context.window_manager
        # Recording samples much more often, so that no played frame is missed.
        self._timer = wm.event_timer_add(0.008 if self.record else 0.1,
                                         window=context.window)
        wm.modal_handler_add(self)


        wm.modal_running = True
        wm.modal_recording = self.record
        return {'RUNNING_MODAL'}

    def start_recording(self, context, xinput_reader_empty):
        scene = context.scene

        # An object outside of the scene is never evaluated, so its keyframes
        # would not play back and nothing could be driven by them.
        if xinput_reader_empty.name not in scene.objects:
            scene.collection.objects.link(xinput_reader_empty)
            self.report({'INFO'}, "Added the XInput Reader empty to the scene "
                                  "so that its keyframes play back")

        if self.from_start:
            # A new take has to be simulated from the beginning, the frames of
            # the previous one would otherwise be replayed over it.
            if reset_rigid_body(context):
                self.report({'INFO'}, "Reset the rigid body simulation")
        elif rigid_body_cache(scene) is not None:
            self.report({'WARNING'}, "Recording onto the end of a take leaves "
                                     "the simulation on the earlier frames")

        if self.clear_previous:
            clear_recording(xinput_reader_empty)

        if self.from_start:
            scene.frame_set(scene.frame_start)

        if context.screen and not context.screen.is_animation_playing:
            try:
                bpy.ops.screen.animation_play()
            except RuntimeError:
                # Recording still works, the frames just have to be played
                # or scrubbed through by hand.
                self.report({'WARNING'}, "Could not start the playback from here, "
                                         "start it yourself to record")

    def stop_recording(self, context):
        try:
            if context.screen and context.screen.is_animation_playing:
                bpy.ops.screen.animation_cancel(restore_frame=False)
        except RuntimeError:
            pass  # Blender is tearing the modal down, the playback goes with it

        xinput_reader_empty = get_reader()
        if xinput_reader_empty is not None:
            apply_recorded_interpolation(xinput_reader_empty)

        if self._first_frame is not None:
            self.report({'INFO'}, "Recorded frames {} to {}".format(
                self._first_frame, self._last_frame))

    def cancel(self, context):
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        wm.modal_running = False
        wm.modal_recording = False

        if self.record:
            self.stop_recording(context)

        if self._gamepad is not None:
            self._gamepad.close()
            self._gamepad = None


class XR_OT_rigid_body_cache(Operator):
    bl_idname = "wm.rigid_body_cache"
    bl_label = "Rigid Body Cache"
    bl_options = {'REGISTER'}

    bake: bpy.props.BoolProperty(
        name="Bake",
        description="Simulate the whole frame range instead of only freeing",
        default=True,
        options={'SKIP_SAVE'},
    )

    @classmethod
    def description(cls, context, properties):
        if properties.bake:
            return ("Throw the rigid body cache away and simulate the whole "
                    "scene frame range again, so that scrubbing and rendering "
                    "match the recorded inputs")
        return ("Throw the rigid body cache away, so that the simulation "
                "responds to the controller again")

    @classmethod
    def poll(cls, context):
        return rigid_body_cache(context.scene) is not None

    def execute(self, context):
        scene = context.scene
        cache = rigid_body_cache(scene)

        if context.window_manager.modal_running:
            self.report({'ERROR'}, "Stop reading the controller first, it would "
                                   "write over the simulated frames")
            return {'CANCELLED'}

        if not scene.rigidbody_world.enabled:
            self.report({'WARNING'}, "The rigid body world is disabled")

        frame = scene.frame_current

        reset_rigid_body(context)

        if not self.bake:
            self.report({'INFO'}, "Freed the rigid body cache")
            return {'FINISHED'}

        # The cache keeps a frame range of its own, which does not follow the
        # scene, and the simulation stops dead at the end of it.
        cache.frame_start = scene.frame_start
        cache.frame_end = scene.frame_end

        with context.temp_override(point_cache=cache):
            scene.frame_set(scene.frame_start)
            bpy.ops.ptcache.bake(bake=True)

        scene.frame_set(frame)

        self.report({'INFO'}, "Baked frames {} to {}".format(
            cache.frame_start, cache.frame_end))
        return {'FINISHED'}


class XR_OT_clear_recording(Operator):
    bl_idname = "wm.clear_recording"
    bl_label = "Clear Recording"
    bl_description = "Delete the keyframes of the recorded controller inputs"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        xinput_reader_empty = get_reader()
        return xinput_reader_empty is not None and bool(get_recording(xinput_reader_empty))

    def execute(self, context):
        clear_recording(get_reader())
        return {'FINISHED'}


class XR_OT_refresh_controllers(Operator):
    bl_idname = "wm.refresh_controllers"
    bl_label = "Refresh Controllers"
    bl_description = "Look for connected controllers again"
    bl_options = {'REGISTER'}

    def execute(self, context):
        refresh_devices()
        return {'FINISHED'}


class XR_OT_drive_nodegroup(Operator):
    bl_idname = "wm.drive_nodegroup"
    bl_label = "Drive Nodegroup"
    bl_description = "Drive nodegroup from controller"
    bl_options = {'REGISTER'}

    def execute(self, context):
        xinput_reader_empty = get_reader()
        controller_inputs = xinput_reader_empty.items()

        xinput_nodegroup_name = 'XInput Reader'
        xinput_nodegroup = bpy.data.node_groups.get(xinput_nodegroup_name)

        #delete nodegroup if it exists
        if xinput_nodegroup is None:
            xinput_nodegroup = bpy.data.node_groups.new(xinput_nodegroup_name, 'GeometryNodeTree')
        
        #get output node
        output_node = None
        for node in xinput_nodegroup.nodes:
            if node.type == 'GROUP_OUTPUT':
                output_node = node
                break

        #create output node if none exists
        if output_node is None:
            output_node = xinput_nodegroup.nodes.new('NodeGroupOutput')
            output_node.location = (0, 0)

        for inputs in controller_inputs:
            if type(xinput_reader_empty[inputs[0]]) == float or int or bool:
                if bpy.app.version[0] == 3:
                    if inputs[0] not in xinput_nodegroup.outputs:
                        xinput_nodegroup.outputs.new("NodeSocketFloat", inputs[0])
                if bpy.app.version[0] >= 4:
                    if inputs[0] not in xinput_nodegroup.interface.items_tree:
                        xinput_nodegroup.interface.new_socket(inputs[0], in_out="OUTPUT", socket_type='NodeSocketFloat')

                #set up driver
                output_socket = output_node.inputs[inputs[0]]
                fcurve = output_socket.driver_add('default_value')
                driver = fcurve.driver
                driver.type = 'AVERAGE'
                if len(driver.variables) == 0:
                    variable = driver.variables.new()
                else:
                    variable = driver.variables[0]
                variable.name = inputs[0]
                variable.type = 'SINGLE_PROP'
                targets = variable.targets[0]
                targets.id_type = 'OBJECT'
                targets.id = xinput_reader_empty
                targets.data_path = f'["{inputs[0]}"]'
                driver.expression = 'var'


        return {'FINISHED'}


#------------------------------------------------------------------------------------------------------------------------------#
#------------------------------------------------------------PANELS------------------------------------------------------------#
#------------------------------------------------------------------------------------------------------------------------------#


class XR_PT_panel(Panel):
    bl_label = "XInput Reader"
    bl_idname = "OBJECT_PT_XInput_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "XInput"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        wm = context.window_manager

        col = layout.column()
        row = col.row()
        row.scale_y = 3

        if wm.modal_running:
            row.enabled = False
            row.operator("wm.monitor_controller", icon="ERROR", text=(
                "Recording, Right Click or Esc to Stop" if wm.modal_recording
                else "Right Click or Esc to Stop"))
        else:
            row.operator("wm.monitor_controller")

            row = col.row()
            row.scale_y = 3
            recorder = row.operator("wm.monitor_controller",
                                    text="Record Controller", icon='REC')
            recorder.record = True
            recorder.from_start = scene.xinput_record_from_start
            recorder.clear_previous = scene.xinput_clear_previous

        options = col.column(align=True)
        options.enabled = not wm.modal_running
        options.prop(scene, "xinput_record_from_start")
        options.prop(scene, "xinput_clear_previous")

        xinput_reader_empty = get_reader()
        recorded = get_recorded_range(xinput_reader_empty) if xinput_reader_empty else None
        if recorded is not None:
            row = col.row()
            row.enabled = not wm.modal_running
            row.label(text="Recorded frames {} to {}".format(*recorded))
            row.operator("wm.clear_recording", text="", icon='TRASH')

        col.separator()
        col.operator("wm.drive_nodegroup")

        col.separator()
        physics = col.column(align=True)
        physics.enabled = not wm.modal_running
        physics.label(text="Rigid Body Cache")
        row = physics.row(align=True)
        row.operator("wm.rigid_body_cache", text="Free", icon='TRASH').bake = False
        row.operator("wm.rigid_body_cache", text="Bake", icon='PHYSICS').bake = True

        cache = rigid_body_cache(scene)
        if cache is None:
            physics.label(text="No rigid body world in this scene", icon='INFO')
        elif cache.is_baked:
            physics.label(text="Baked frames {} to {}".format(
                cache.frame_start, cache.frame_end), icon='CHECKMARK')
        elif (cache.frame_start, cache.frame_end) != (scene.frame_start, scene.frame_end):
            physics.label(text="Cache range is {} to {}, baking fixes it".format(
                cache.frame_start, cache.frame_end), icon='ERROR')

        xinput_reader_empty = get_reader()
        if xinput_reader_empty is not None:
            controller_inputs = xinput_reader_empty.items()

            box = layout.box()
            box.label(text="Controller Inputs")
            param_count = 0
            for controller_input in controller_inputs:
                if type(xinput_reader_empty[controller_input[0]]) == float or int or bool:
                    row = box.row()
                    prop_name = controller_input[0]
                    row.prop(xinput_reader_empty, f'["{prop_name}"]')
                    param_count += 1

#--------------------------------------------------------------------------------------------------------------------------------#
#------------------------------------------------------------REGISTER------------------------------------------------------------#
#--------------------------------------------------------------------------------------------------------------------------------#


classes = (
    XR_OT_monitor_controller,
    XR_OT_rigid_body_cache,
    XR_OT_clear_recording,
    XR_OT_refresh_controllers,
    XR_OT_drive_nodegroup,
    XR_PT_panel,
    XR_PT_preferences_panel,
)

def register():
    bpy.types.WindowManager.modal_running = bpy.props.BoolProperty(default=False)
    bpy.types.WindowManager.modal_recording = bpy.props.BoolProperty(default=False)

    bpy.types.Scene.xinput_record_from_start = bpy.props.BoolProperty(
        name="Start From First Frame",
        description="Jump back to the first frame before recording, so that "
                    "simulations are run from their beginning",
        default=True,
    )
    bpy.types.Scene.xinput_clear_previous = bpy.props.BoolProperty(
        name="Clear Previous Recording",
        description="Throw away an earlier recording instead of recording over "
                    "the part of it that is played through",
        default=True,
    )

    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)


def unregister():
    del bpy.types.WindowManager.modal_running
    del bpy.types.WindowManager.modal_recording
    del bpy.types.Scene.xinput_record_from_start
    del bpy.types.Scene.xinput_clear_previous

    from bpy.utils import unregister_class
    for cls in reversed(classes):
        unregister_class(cls)
