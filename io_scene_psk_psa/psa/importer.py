import bpy
import os
import numpy as np
from mathutils import Vector, Quaternion, Matrix
from .data import Psa
from typing import List, AnyStr, Optional
from bpy.types import Operator, Action, UIList, PropertyGroup, Panel, Armature, FileSelectParams
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy.props import StringProperty, BoolProperty, CollectionProperty, PointerProperty, IntProperty
from .reader import PsaReader


class PsaImportOptions(object):
    def __init__(self):
        self.should_clean_keys = True
        self.should_use_fake_user = False
        self.should_stash = False
        self.sequence_names = []


class PsaImporter(object):
    def __init__(self):
        pass

    def import_psa(self, psa_reader: PsaReader, armature_object, options: PsaImportOptions):
        sequences = map(lambda x: psa_reader.sequences[x], options.sequence_names)
        armature_data = armature_object.data

        class ImportBone(object):
            def __init__(self, psa_bone: Psa.Bone):
                self.psa_bone: Psa.Bone = psa_bone
                self.parent: Optional[ImportBone] = None
                self.armature_bone = None
                self.pose_bone = None
                self.orig_loc: Vector = Vector()
                self.orig_quat: Quaternion = Quaternion()
                self.post_quat: Quaternion = Quaternion()
                self.fcurves = []

        def calculate_fcurve_data(import_bone: ImportBone, key_data: []):
            # Convert world-space transforms to local-space transforms.
            key_rotation = Quaternion(key_data[0:4])
            key_location = Vector(key_data[4:])
            q = import_bone.post_quat.copy()
            q.rotate(import_bone.orig_quat)
            quat = q
            q = import_bone.post_quat.copy()
            if import_bone.parent is None:
                q.rotate(key_rotation.conjugated())
            else:
                q.rotate(key_rotation)
            quat.rotate(q.conjugated())
            loc = key_location - import_bone.orig_loc
            loc.rotate(import_bone.post_quat.conjugated())
            return quat.w, quat.x, quat.y, quat.z, loc.x, loc.y, loc.z

        # Create an index mapping from bones in the PSA to bones in the target armature.
        psa_to_armature_bone_indices = {}
        armature_bone_names = [x.name for x in armature_data.bones]
        psa_bone_names = []
        for psa_bone_index, psa_bone in enumerate(psa_reader.bones):
            psa_bone_name = psa_bone.name.decode('windows-1252')
            psa_bone_names.append(psa_bone_name)
            try:
                psa_to_armature_bone_indices[psa_bone_index] = armature_bone_names.index(psa_bone_name)
            except ValueError:
                pass

        # Report if there are missing bones in the target armature.
        missing_bone_names = set(psa_bone_names).difference(set(armature_bone_names))
        if len(missing_bone_names) > 0:
            print(f'The armature object \'{armature_object.name}\' is missing the following bones that exist in the PSA:')
            print(list(sorted(missing_bone_names)))
        del armature_bone_names

        # Create intermediate bone data for import operations.
        import_bones = []
        import_bones_dict = dict()

        for psa_bone_index, psa_bone in enumerate(psa_reader.bones):
            bone_name = psa_bone.name.decode('windows-1252')
            if psa_bone_index not in psa_to_armature_bone_indices:  # TODO: replace with bone_name in armature_data.bones
                # PSA bone does not map to armature bone, skip it and leave an empty bone in its place.
                import_bones.append(None)
                continue
            import_bone = ImportBone(psa_bone)
            import_bone.armature_bone = armature_data.bones[bone_name]
            import_bone.pose_bone = armature_object.pose.bones[bone_name]
            import_bones_dict[bone_name] = import_bone
            import_bones.append(import_bone)

        for import_bone in filter(lambda x: x is not None, import_bones):
            armature_bone = import_bone.armature_bone
            if armature_bone.parent is not None and armature_bone.parent.name in psa_bone_names:
                import_bone.parent = import_bones_dict[armature_bone.parent.name]
            # Calculate the original location & rotation of each bone (in world-space maybe?)
            if armature_bone.get('orig_quat') is not None:
                # TODO: ideally we don't rely on bone auxiliary data like this, the non-aux data path is incorrect (animations are flipped 180 around Z)
                import_bone.orig_quat = Quaternion(armature_bone['orig_quat'])
                import_bone.orig_loc = Vector(armature_bone['orig_loc'])
                import_bone.post_quat = Quaternion(armature_bone['post_quat'])
            else:
                if import_bone.parent is not None:
                    import_bone.orig_loc = armature_bone.matrix_local.translation - armature_bone.parent.matrix_local.translation
                    import_bone.orig_loc.rotate(armature_bone.parent.matrix_local.to_quaternion().conjugated())
                    import_bone.orig_quat = armature_bone.matrix_local.to_quaternion()
                    import_bone.orig_quat.rotate(armature_bone.parent.matrix_local.to_quaternion().conjugated())
                    import_bone.orig_quat.conjugate()
                else:
                    import_bone.orig_loc = armature_bone.matrix_local.translation.copy()
                    import_bone.orig_quat = armature_bone.matrix_local.to_quaternion()
                import_bone.post_quat = import_bone.orig_quat.conjugated()

        # Create and populate the data for new sequences.
        actions = []
        for sequence in sequences:
            # Add the action.
            action = bpy.data.actions.new(name=sequence.name.decode())
            action.use_fake_user = options.should_use_fake_user

            # Create f-curves for the rotation and location of each bone.
            for psa_bone_index, armature_bone_index in psa_to_armature_bone_indices.items():
                import_bone = import_bones[psa_bone_index]
                pose_bone = import_bone.pose_bone
                rotation_data_path = pose_bone.path_from_id('rotation_quaternion')
                location_data_path = pose_bone.path_from_id('location')
                import_bone.fcurves = [
                    action.fcurves.new(rotation_data_path, index=0),  # Qw
                    action.fcurves.new(rotation_data_path, index=1),  # Qx
                    action.fcurves.new(rotation_data_path, index=2),  # Qy
                    action.fcurves.new(rotation_data_path, index=3),  # Qz
                    action.fcurves.new(location_data_path, index=0),  # Lx
                    action.fcurves.new(location_data_path, index=1),  # Ly
                    action.fcurves.new(location_data_path, index=2),  # Lz
                ]

            sequence_name = sequence.name.decode('windows-1252')

            # Read the sequence data matrix from the PSA.
            sequence_data_matrix = psa_reader.read_sequence_data_matrix(sequence_name)
            keyframe_write_matrix = np.ones(sequence_data_matrix.shape, dtype=np.int8)

            # Convert the sequence's data from world-space to local-space.
            for bone_index, import_bone in enumerate(import_bones):
                if import_bone is None:
                    continue
                for frame_index in range(sequence.frame_count):
                    # This bone has writeable keyframes for this frame.
                    key_data = sequence_data_matrix[frame_index, bone_index]
                    # Calculate the local-space key data for the bone.
                    sequence_data_matrix[frame_index, bone_index] = calculate_fcurve_data(import_bone, key_data)

            # Clean the keyframe data. This is accomplished by writing zeroes to the write matrix when there is an
            # insufficiently large change in the data from frame-to-frame.
            if options.should_clean_keys:
                threshold = 0.001
                for bone_index, import_bone in enumerate(import_bones):
                    if import_bone is None:
                        continue
                    for fcurve_index in range(len(import_bone.fcurves)):
                        # Get all the keyframe data for the bone's f-curve data from the sequence data matrix.
                        fcurve_frame_data = sequence_data_matrix[:, bone_index, fcurve_index]
                        last_written_datum = 0
                        for frame_index, datum in enumerate(fcurve_frame_data):
                            # If the f-curve data is not different enough to the last written frame, un-mark this data for writing.
                            if frame_index > 0 and abs(datum - last_written_datum) < threshold:
                                keyframe_write_matrix[frame_index, bone_index, fcurve_index] = 0
                            else:
                                last_written_datum = datum

            # Write the keyframes out!
            for frame_index in range(sequence.frame_count):
                for bone_index, import_bone in enumerate(import_bones):
                    if import_bone is None:
                        continue
                    bone_has_writeable_keyframes = any(keyframe_write_matrix[frame_index, bone_index])
                    if bone_has_writeable_keyframes:
                        # This bone has writeable keyframes for this frame.
                        key_data = sequence_data_matrix[frame_index, bone_index]
                        for fcurve, should_write, datum in zip(import_bone.fcurves, keyframe_write_matrix[frame_index, bone_index], key_data):
                            if should_write:
                                fcurve.keyframe_points.insert(frame_index, datum, options={'FAST'})

            actions.append(action)

        # If the user specifies, store the new animations as strips on a non-contributing NLA stack.
        if options.should_stash:
            if armature_object.animation_data is None:
                armature_object.animation_data_create()
            for action in actions:
                nla_track = armature_object.animation_data.nla_tracks.new()
                nla_track.name = action.name
                nla_track.mute = True
                nla_track.strips.new(name=action.name, start=0, action=action)


class PsaImportPsaBoneItem(PropertyGroup):
    bone_name: StringProperty()

    @property
    def name(self):
        return self.bone_name


class PsaImportActionListItem(PropertyGroup):
    action_name: StringProperty()
    frame_count: IntProperty()
    is_selected: BoolProperty(default=False)

    @property
    def name(self):
        return self.action_name


def on_psa_file_path_updated(property, context):
    property_group = context.scene.psa_import
    property_group.action_list.clear()
    property_group.psa_bones.clear()
    try:
        # Read the file and populate the action list.
        p = os.path.abspath(property_group.psa_file_path)
        psa_reader = PsaReader(p)
        for sequence in psa_reader.sequences.values():
            item = property_group.action_list.add()
            item.action_name = sequence.name.decode('windows-1252')
            item.frame_count = sequence.frame_count
            item.is_selected = True
        for psa_bone in psa_reader.bones:
            item = property_group.psa_bones.add()
            item.bone_name = psa_bone.name
    except IOError as e:
        # TODO: set an error somewhere so the user knows the PSA could not be read.
        pass


class PsaImportPropertyGroup(PropertyGroup):
    psa_file_path: StringProperty(default='', update=on_psa_file_path_updated, name='PSA File Path')
    psa_bones: CollectionProperty(type=PsaImportPsaBoneItem)
    action_list: CollectionProperty(type=PsaImportActionListItem)
    action_list_index: IntProperty(name='', default=0)
    action_filter_name: StringProperty(default='')
    should_clean_keys: BoolProperty(default=True, name='Clean Keyframes', description='Exclude unnecessary keyframes from being written to the actions.')
    should_use_fake_user: BoolProperty(default=True, name='Fake User', description='Assign each imported action a fake user so that the data block is saved even it has no users.')
    should_stash: BoolProperty(default=False, name='Stash', description='Stash each imported action as a strip on a new non-contributing NLA track')


class PSA_UL_ImportActionList(UIList):

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        split = row.split(align=True, factor=0.75)
        action_col = split.row(align=True)
        action_col.alignment = 'LEFT'
        action_col.prop(item, 'is_selected', icon_only=True)
        action_col.label(text=item.action_name)

    def draw_filter(self, context, layout):
        row = layout.row()
        subrow = row.row(align=True)
        subrow.prop(self, 'filter_name', text="")
        subrow.prop(self, 'use_filter_invert', text="", icon='ARROW_LEFTRIGHT')
        subrow = row.row(align=True)
        subrow.prop(self, 'use_filter_sort_reverse', text='', icon='SORT_ASC')

    def filter_items(self, context, data, property):
        actions = getattr(data, property)
        flt_flags = []
        flt_neworder = []
        if self.filter_name:
            flt_flags = bpy.types.UI_UL_list.filter_items_by_name(
                self.filter_name,
                self.bitflag_filter_item,
                actions,
                'action_name',
                reverse=self.use_filter_invert
            )
        return flt_flags, flt_neworder


class PsaImportSelectAll(bpy.types.Operator):
    bl_idname = 'psa_import.actions_select_all'
    bl_label = 'All'
    bl_description = 'Select all actions'

    @classmethod
    def poll(cls, context):
        property_group = context.scene.psa_import
        action_list = property_group.action_list
        has_unselected_actions = any(map(lambda action: not action.is_selected, action_list))
        return len(action_list) > 0 and has_unselected_actions

    def execute(self, context):
        property_group = context.scene.psa_import
        for action in property_group.action_list:
            action.is_selected = True
        return {'FINISHED'}


class PsaImportDeselectAll(bpy.types.Operator):
    bl_idname = 'psa_import.actions_deselect_all'
    bl_label = 'None'
    bl_description = 'Deselect all actions'

    @classmethod
    def poll(cls, context):
        property_group = context.scene.psa_import
        action_list = property_group.action_list
        has_selected_actions = any(map(lambda action: action.is_selected, action_list))
        return len(action_list) > 0 and has_selected_actions

    def execute(self, context):
        property_group = context.scene.psa_import
        for action in property_group.action_list:
            action.is_selected = False
        return {'FINISHED'}


class PSA_PT_ImportPanel(Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_label = 'PSA Import'
    bl_context = 'data'
    bl_category = 'PSA Import'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.object.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        property_group = context.scene.psa_import

        row = layout.row()
        row.prop(property_group, 'psa_file_path', text='')
        row.enabled = False

        row = layout.row()
        row.operator('psa_import.select_file', text='Select PSA File', icon='FILEBROWSER')

        if len(property_group.action_list) > 0:
            box = layout.box()
            box.label(text=f'Actions ({len(property_group.action_list)})', icon='ACTION')
            row = box.row()
            rows = max(3, min(len(property_group.action_list), 10))
            row.template_list('PSA_UL_ImportActionList', '', property_group, 'action_list', property_group, 'action_list_index', rows=rows)
            row = box.row(align=True)
            row.label(text='Select')
            row.operator('psa_import.actions_select_all', text='All')
            row.operator('psa_import.actions_deselect_all', text='None')

        row = layout.row()
        row.prop(property_group, 'should_clean_keys')

        # DATA
        row = layout.row()
        row.prop(property_group, 'should_use_fake_user')
        row.prop(property_group, 'should_stash')

        layout.operator('psa_import.import', text=f'Import')


class PsaImportSelectFile(Operator):
    bl_idname = 'psa_import.select_file'
    bl_label = 'Select'
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = 'Select a PSA file from which to import animations'
    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.psa", options={'HIDDEN'})

    def execute(self, context):
        context.scene.psa_import.psa_file_path = self.filepath
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class PsaImportOperator(Operator):
    bl_idname = 'psa_import.import'
    bl_label = 'Import'
    bl_description = 'Import the selected animations into the scene as actions'

    @classmethod
    def poll(cls, context):
        property_group = context.scene.psa_import
        active_object = context.view_layer.objects.active
        action_list = property_group.action_list
        has_selected_actions = any(map(lambda action: action.is_selected, action_list))
        return has_selected_actions and active_object is not None and active_object.type == 'ARMATURE'

    def execute(self, context):
        property_group = context.scene.psa_import
        psa_reader = PsaReader(property_group.psa_file_path)
        sequence_names = [x.action_name for x in property_group.action_list if x.is_selected]
        options = PsaImportOptions()
        options.sequence_names = sequence_names
        options.should_clean_keys = property_group.should_clean_keys
        options.should_use_fake_user = property_group.should_use_fake_user
        options.should_stash = property_group.should_stash
        PsaImporter().import_psa(psa_reader, context.view_layer.objects.active, options)
        self.report({'INFO'}, f'Imported {len(sequence_names)} action(s)')
        return {'FINISHED'}


class PsaImportFileSelectOperator(Operator, ImportHelper):
    bl_idname = 'psa_import.file_select'
    bl_label = 'File Select'
    filename_ext = '.psa'
    filter_glob: StringProperty(default='*.psa', options={'HIDDEN'})
    filepath: StringProperty(
        name='File Path',
        description='File path used for importing the PSA file',
        maxlen=1024,
        default='')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        property_group = context.scene.psa_import
        property_group.psa_file_path = self.filepath
        # Load the sequence names from the selected file
        return {'FINISHED'}


__classes__ = [
    PsaImportPsaBoneItem,
    PsaImportActionListItem,
    PsaImportPropertyGroup,
    PSA_UL_ImportActionList,
    PsaImportSelectAll,
    PsaImportDeselectAll,
    PSA_PT_ImportPanel,
    PsaImportOperator,
    PsaImportFileSelectOperator,
    PsaImportSelectFile,
]