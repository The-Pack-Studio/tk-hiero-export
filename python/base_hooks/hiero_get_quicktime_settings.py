# Copyright (c) 2018 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

import sgtk

HookBaseClass = sgtk.get_hook_baseclass()


class HieroGetQuicktimeSettings(HookBaseClass):
    """
    This class defines a hook that allows for customization of encoding
    settings for any Quicktimes written by the export process.
    """

    def execute(self, for_shotgun, **kwargs):
        """
        Gets encoding settings for Quicktimes generated by the export process.

        :param bool for_shotgun: Whether the settings are being gathered for
            Quicktime output intended for use within the Shotgun web app.

        :returns: A tuple, where the first item is the file_type of a Nuke
            write node, and the second item is a dictionary of knob names and
            values.
        :rtype: tuple
        """
        pass
