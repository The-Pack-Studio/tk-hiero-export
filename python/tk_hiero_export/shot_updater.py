# Copyright (c) 2013 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import re
import hiero.core
import nuke
from hiero.exporters import FnShotExporter

from .base import ShotgunHieroObjectBase
from .collating_exporter import CollatingExporter

from . import (
    HieroGetShot,
    HieroUpdateShot,
    HieroUpdateCuts,
)

class ShotgunShotUpdater(
    ShotgunHieroObjectBase, FnShotExporter.ShotTask, CollatingExporter
):

    """
    Ensures that Shots and Sequences exist in Shotgun
    """

    def __init__(self, initDict):
        FnShotExporter.ShotTask.__init__(self, initDict)
        CollatingExporter.__init__(self)
        self._cut_order = None

    def get_cut_item_data(self):
        """
        Return some computed values for use when creating cut items.

        The values correspond to the exported version created on disk.
        """

        (head_in, tail_out) = self.collatedOutputRange(clampToSource=False) # these values are not correct for negative retimed shots, I'm overriding these, see below

        handles = self._cutHandles if self._cutHandles is not None else 0
        in_handle = handles
        out_handle = handles

        # get the frame offset specified in the export options
        startFrame = self._startFrame or 0

        # these are the source in/out frames. we'll use them to determine if we
        # have enough frames to account for the handles. versions of
        # hiero/nukestudio handle missing handles differently
        source_in = int(self._item.sourceIn())
        source_out = int(self._item.sourceOut())

        if self._has_nuke_backend() and source_in < in_handle:
            # newer versions of the hiero/nukestudio. no black frames will be
            # written to disk for the head when not enough source for the in
            # handle. the in/out should be correct. but the start handle is
            # limited by the in value. the source in point is within the
            # specified handles.
            in_handle = source_in

            # NOTE: even new versions of hiero/nukestudio will write black
            # frames for insuffient tail handles. so we don't need to account
            # for that case here.

        # "cut_length" is a boolean set on the updater by the shot processor.
        # it signifies whether the transcode task will write the cut length
        # to disk (True) or if it will write the full source to disk (False)
        if self.is_cut_length_export():
            cut_in = head_in + in_handle
            cut_out = tail_out - out_handle
        else:
            # cut_in = source_in + self._item.source().sourceIn()
            # cut_out = source_out + self._item.source().sourceIn()
            cut_in = source_in
            cut_out = source_out

            # account for any custom start frame
            cut_in += startFrame
            cut_out += startFrame

        '''
        Donat : overriding cut info because I get incorrect results for negative retimed shots
        In the case of a cut lenght export with hanldes and a custom start frame 
        '''
        if self._startFrame and self._cutHandles and self.is_cut_length_export():
            head_in = startFrame
            cut_in = startFrame + in_handle
            cut_out = cut_in + self._item.duration() - 1
            tail_out = cut_out + out_handle
            self.app.log_debug('Donat : overriding cut info with values : '
                            'head_in=%s, cut_in=%s, cut_out=%s, tail_out=%s' % (head_in, cut_in, cut_out, tail_out))



        # get the edit in/out points from the timeline
        edit_in = self._item.timelineIn()
        edit_out = self._item.timelineOut()

        # account for custom start code in the hiero timeline
        seq = self._item.sequence()
        edit_in += seq.timecodeStart()
        edit_out += seq.timecodeStart()

        cut_duration = cut_out - cut_in + 1
        edit_duration = edit_out - edit_in + 1

        if cut_duration != edit_duration:
            self.app.log_warning(
                "It looks like the shot %s has a retime applied. SG cuts do "
                "not support retimes." % (self.clipName(),)
            )

        working_duration = tail_out - head_in + 1

        if not self._has_nuke_backend() and self.isCollated():
            # undo the offset that is automatically added when collating.
            # this is only required in older versions of hiero
            head_in -= self.HEAD_ROOM_OFFSET
            tail_out -= self.HEAD_ROOM_OFFSET

        # return the computed cut information
        return {
            "cut_item_in": cut_in,
            "cut_item_out": cut_out,
            "cut_item_duration": cut_duration,
            "edit_in": edit_in,
            "edit_out": edit_out,
            "edit_duration": edit_duration,
            "head_in": head_in,
            "tail_out": tail_out,
            "working_duration": working_duration,
        }

    def taskStep(self):
        """
        Execution payload.
        """
        # Only process actual shots... so uncollated items and hero collated items
        if self.isCollated() and not self.isHero():
            return False

        # Donat : only update the shot info on SG if the item has the correct tag defined in the settings of the app
        tags_names_list = [ tag.name() for tag in self._item.tags() ]
        shot_update_tag = self.app.get_setting("shot_update_tag")
        if not shot_update_tag in tags_names_list:
            self.app.log_debug("Donat : No '{}' tag on this item, skipping shot update.".format(shot_update_tag))
            return False
        


        # execute base class
        FnShotExporter.ShotTask.taskStep(self)

        # call the preprocess hook to get extra values
        if self.app.shot_count == 0:
            self.app.preprocess_data = {}

        sg_shot = self.app.execute_hook(
            "hook_get_shot",
            task=self,
            item=self._item,
            data=self.app.preprocess_data,
            base_class=HieroGetShot,
        )

        # clean up the dict
        shot_id = sg_shot["id"]
        del sg_shot["id"]
        shot_type = sg_shot["type"]
        del sg_shot["type"]

        # The cut order may have been set by the processor. Otherwise keep old behavior.
        cut_order = self.app.shot_count + 1
        if self._cut_order:
            cut_order = self._cut_order

        # update the frame range
        sg_shot["sg_cut_order"] = cut_order

        # get cut info
        cut_info = self.get_cut_item_data()

        head_in = cut_info["head_in"]
        tail_out = cut_info["tail_out"]
        cut_in = cut_info["cut_item_in"]
        cut_out = cut_info["cut_item_out"]
        cut_duration = cut_info["cut_item_duration"]
        working_duration = cut_info["working_duration"]

        self.app.log_debug("Head/Tail from Hiero: %s, %s" % (head_in, tail_out))

        if self.isCollated():

            if self.is_cut_length_export():
                # nothing to do here. the default calculation above is enough.
                self.app.log_debug("Exporting... collated, cut length.")

                # Log cut length collate metric
                try:
                    self.app.log_metric("Collate/Cut Length", log_version=True)
                except:
                    # ingore any errors. ex: metrics logging not supported
                    pass

            else:
                self.app.log_debug("Exporting... collated, clip length.")

                # NOTE: Hiero crashes when trying to collate with a
                # custom start frame. so this will only work for source start
                # frame.

                # the head/in out values should be the first and last frames of
                # the source, but they're not. they're actually the values we
                # expect for the cut in/out.
                cut_in = head_in
                cut_out = tail_out

                # ensure head/tail match the entire clip (clip length export)
                head_in = 0
                tail_out = self._clip.duration() - 1

                # get the frame offset specified in the export options
                start_frame = self._startFrame or 0

                # account for a custom start frame if/when clip length collate
                # works on custom start frame.
                head_in += start_frame
                tail_out += start_frame
                cut_in += start_frame
                cut_out += start_frame

                # since we've set the head/tail, recalculate the working
                # duration to make sure it is correct
                working_duration = tail_out - head_in + 1

                # since we've set the cut in/out, recalculate the cut duration
                # to make sure it is correct
                cut_duration = cut_out - cut_in + 1

                # Log clip length collate metric
                try:
                    self.app.log_metric("Collate/Clip Length", log_version=True)
                except:
                    # ingore any errors. ex: metrics logging not supported
                    pass

        else:
            # regular export. values we have are good. just log it
            if self.is_cut_length_export():
                self.app.log_debug("Exporting... cut length.")
            else:
                # the cut in/out should already be correct here. just log
                self.app.log_debug("Exporting... clip length.")

        # update the frame range
        sg_shot["sg_head_in"] = head_in
        sg_shot["sg_cut_in"] = cut_in
        sg_shot["sg_cut_out"] = cut_out
        sg_shot["sg_tail_out"] = tail_out
        sg_shot["sg_cut_duration"] = cut_duration
        sg_shot["sg_working_duration"] = working_duration

        # Donat : add source cut in timecode and frame start
        sg_shot["sg_source_start_timecode"] = self.get_source_in_timecode(self._item)
        sg_shot["sg_source_start_frame"] = cut_in
        rec_in_timecode, rec_out_timecode = self.get_record_timecodes(self._item)
        sg_shot["sg_record_in_timecode"] = rec_in_timecode
        sg_shot["sg_record_out_timecode"] = rec_out_timecode


        # Donat : add tags
        sg_tags = []
        tags_app = self.app.engine.apps.get("tk-hiero-tags")
        if tags_app:
            sg_tags = tags_app.get_sg_tags(self._item)
            sg_shot["sg_project_tags"] = sg_tags
        else:
            self.parent.log_info("The 'tk-hiero-tags' app is not running. Will not send tags to SG")


        # Donat add the colorspace of the clip's to the camera colorspace in SG database
        clip_source_colorspace = self.get_source_colorspace(self._item)
        # fetch valid values configured on the sg_camera_colorspace
        valid_shotgun_colorspaces = self.app.shotgun.schema_field_read('Shot', 'sg_camera_colorspace')['sg_camera_colorspace']['properties']['valid_values']['value']
        if clip_source_colorspace in valid_shotgun_colorspaces:
            sg_shot["sg_camera_colorspace"] = clip_source_colorspace
        else:
            self.app.log_debug("The clip source colorspace: %s is not found on the list of colorspace values in SG : %s" % (clip_source_colorspace, valid_shotgun_colorspaces))


        # get status from the hiero tags
        status = None
        status_map = dict(self._preset.properties()["sg_status_hiero_tags"])
        for tag in self._item.tags():
            if tag.name() in status_map:
                status = status_map[tag.name()]
                break
        if status:
            sg_shot["sg_status_list"] = status

        # get task template from the tags
        template = None
        template_map = dict(self._preset.properties()["task_template_map"])
        for tag in self._item.tags():
            if tag.name() in template_map:
                template = self.app.tank.shotgun.find_one(
                    "TaskTemplate",
                    [
                        ["entity_type", "is", shot_type],
                        ["code", "is", template_map[tag.name()]],
                    ],
                )
                break

        # if there are no associated, assign default template...
        if template is None:
            default_template = self.app.get_setting("default_task_template")
            if default_template:
                template = self.app.tank.shotgun.find_one(
                    "TaskTemplate",
                    [
                        ["entity_type", "is", shot_type],
                        ["code", "is", default_template],
                    ],
                )

        if template is not None:
            sg_shot["task_template"] = template

        # commit the changes and update the thumbnail
        self.app.execute_hook_method(
            "hook_update_shot",
            "update_shotgun_shot_entity",
            entity_type=shot_type,
            entity_id=shot_id,
            entity_data=sg_shot,
            preset_properties=self._preset.properties(),
            base_class=HieroUpdateShot,
        )

        # create the directory structure
        self.app.execute_hook_method(
            "hook_update_shot",
            "create_filesystem_structure",
            entity_type=shot_type,
            entity_id=shot_id,
            preset_properties=self._preset.properties(),
            base_class=HieroUpdateShot,
        )

        # return without error
        self.app.log_info("Updated %s %s" % (shot_type, self.shotName()))

        # keep shot count
        self.app.shot_count += 1

        # create the CutItem with the data populated by the shot processor
        cut = None

        if hasattr(self, "_cut_item_data"):
            cut_item_data = self._cut_item_data
            cut_item = self.app.execute_hook_method(
                "hook_update_cuts",
                "create_cut_item",
                cut_item_data=cut_item_data,
                preset_properties=self._preset.properties(),
                base_class=HieroUpdateCuts,
            )

            # If a CutItem entity wasn't created by the hook method, then it
            # will have returned a None.
            if cut_item is not None:
                # update the object's cut item data to include the new info
                self._cut_item_data.update(cut_item)

                cut = cut_item["cut"]

        # see if this task has been designated to update the Cut thumbnail
        if cut and hasattr(self, "_create_cut_thumbnail"):
            thumbnail = self.app.execute_hook_method(
                "hook_update_cuts",
                "get_cut_thumbnail",
                cut=cut,
                task_item=self._item,
                preset_properties=self._preset.properties(),
                base_class=HieroUpdateCuts,
            )

            if thumbnail:
                # found one, uplaod to sg for the cut
                self._upload_thumbnail_to_sg(cut, thumbnail)

        # return false to indicate success
        return False

    def is_cut_length_export(self):
        """
        Returns ``True`` if this task has the "Cut Length" option checked.

        This is set by the shot processor.
        """
        return hasattr(self, "_cut_length") and self._cut_length

    def get_source_in_timecode(self, trackItem):
        """
        Gets the clips source timecode for the first visible frame
        WARNING : This is not correct when a TimeWarp soft effect is applied on the clip
        (NukeStudio's spreadsheet view is also not correct in that case)
        """

        fps = trackItem.parent().parent().framerate()
        clip = trackItem.source()
        clipstartTimeCode = clip.timecodeStart()
        source_in_timecode = hiero.core.Timecode.timeToString(clipstartTimeCode+trackItem.sourceIn(), fps, hiero.core.Timecode.kDisplayTimecode)
        
        return source_in_timecode


    def get_record_timecodes(self, trackItem):
        """
        Gets the clips record in and record out timecode
        """

        timeline = trackItem.parentSequence()
        timeline_fps = timeline.framerate()
        timeline_frame_start = timeline.timecodeStart()

        clip_timeline_in = trackItem.timelineIn()
        clip_timeline_out = trackItem.timelineOut()

        rec_in_timecode = hiero.core.Timecode.timeToString((clip_timeline_in + timeline_frame_start), timeline_fps, hiero.core.Timecode.kDisplayTimecode)
        rec_out_timecode = hiero.core.Timecode.timeToString((clip_timeline_out + timeline_frame_start + 1), timeline_fps, hiero.core.Timecode.kDisplayTimecode)


        return (rec_in_timecode, rec_out_timecode)


    def get_source_colorspace(self, trackItem):

        readNode = trackItem.source().readNode()

        # Create a dict: Each key/value pair is a role name/colorspace name
        # If a colorspace has no role, then key and value will be the colorspace name
        cs_knob = readNode.knob('colorspace')
        colorspace_list = nuke.getColorspaceList(cs_knob)

        cs_roles_dict = {}

        for colorspace in colorspace_list:
            role_colorspace_match = re.match(r"(\w+)[^(]+\((.+)\)", colorspace)
            if role_colorspace_match:
                rolename = role_colorspace_match.group(1)
                colorspace_name = role_colorspace_match.group(2)
            else : rolename = colorspace_name = colorspace

            cs_roles_dict.update({rolename: colorspace_name})

        # When a read node colorspace knob is set to: 'default (something)',
        # sometimes, fetching the value of the knob returns 'default' instead of 'default (something)'
        # After asking the Foundry for help, they told me to use the forceValidate() method on
        # the read node prior to getting the colorspace knob value. This seems to correct the issue   
        readNode.forceValidate()
        colorspace = readNode["colorspace"].value()
        self.app.log_debug("The clip source colorspace is: %s" % colorspace)

        # First check the special case if it is the 'default (xxx)' from Nuke, NOT from the default role of the OCIO config
        # in that case the colorspace will be 'default (xxx)', in all other cases it will only be a single string
        # with no parenthesis. The 'xxx' inside 'default (xxx)' could also be a role name
        default_match = re.match(r"default \((.+)\)", colorspace)
        if default_match:
            colorspace = default_match.group(1)

        actual_colorspace = cs_roles_dict[colorspace]


        self.app.log_debug("The clip source actual colorspace is: %s" % actual_colorspace)

        return actual_colorspace



class ShotgunShotUpdaterPreset(ShotgunHieroObjectBase, hiero.core.TaskPresetBase):
    """
    Settings preset
    """

    def __init__(self, name, properties):
        hiero.core.TaskPresetBase.__init__(self, ShotgunShotUpdater, name)
        self.properties().update(properties)

    def supportedItems(self):
        return hiero.core.TaskPresetBase.kAllItems
