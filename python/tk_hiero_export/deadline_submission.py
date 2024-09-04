import os
import sys
import ast
import re
import platform
import traceback
import subprocess

try:
    # Nuke 11 onwards uses PySide2
    from PySide2.QtCore import *
    from PySide2.QtGui import *
    from PySide2.QtWidgets import *
    print("Deadline: Using PySide2")
except:
    from PySide.QtCore import *
    from PySide.QtGui import *
    print("Deadline: Using PySide")

import hiero.core
from hiero.core import *
from hiero.exporters.FnSubmission import Submission

import hiero.ui
from hiero.ui import *

import json

import tank
import sgtk
import sgtk.util

from .base import ShotgunHieroObjectBase
from .collating_exporter import CollatingExporter, CollatedShotPreset

from . import (
    HieroGetQuicktimeSettings,
    HieroGetShot,
    HieroUpdateVersionData,
    HieroGetExtraPublishData,
    HieroPostVersionCreation,
)

def GetDeadlineCommand():
    deadlineBin = ""
    try:
        deadlineBin = os.environ['DEADLINE_PATH']
    except KeyError:
        #if the error is a key error it means that DEADLINE_PATH is not set. however Deadline command may be in the PATH or on OSX it could be in the file /Users/Shared/Thinkbox/DEADLINE_PATH
        pass
        
    # On OSX, we look for the DEADLINE_PATH file if the environment variable does not exist.
    if deadlineBin == "" and  os.path.exists( "/Users/Shared/Thinkbox/DEADLINE_PATH" ):
        with open( "/Users/Shared/Thinkbox/DEADLINE_PATH" ) as f:
            deadlineBin = f.read().strip()

    deadlineCommand = os.path.join(deadlineBin, "deadlinecommand")
    
    return deadlineCommand

def CallDeadlineCommand( arguments, hideWindow=True ):
    deadlineCommand = GetDeadlineCommand()
    
    startupinfo = None
    if hideWindow and os.name == 'nt':
        # Python 2.6 has subprocess.STARTF_USESHOWWINDOW, and Python 2.7 has subprocess._subprocess.STARTF_USESHOWWINDOW, so check for both.
        if hasattr( subprocess, '_subprocess' ) and hasattr( subprocess._subprocess, 'STARTF_USESHOWWINDOW' ):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess._subprocess.STARTF_USESHOWWINDOW
        elif hasattr( subprocess, 'STARTF_USESHOWWINDOW' ):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    
    environment = {}
    for key in os.environ.keys():
        environment[key] = str(os.environ[key])
        
    # Need to set the PATH, cuz windows seems to load DLLs from the PATH earlier that cwd....
    if os.name == 'nt':
        deadlineCommandDir = os.path.dirname( deadlineCommand )
        if not deadlineCommandDir == "" :
            environment['PATH'] = deadlineCommandDir + os.pathsep + os.environ['PATH']
    
    arguments.insert( 0, deadlineCommand)
    
    # Specifying PIPE for all handles to workaround a Python bug on Windows. The unused handles are then closed immediatley afterwards.
    proc = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo, env=environment)
    proc.stdin.close()
    proc.stderr.close()

    output = proc.stdout.read()
    
    return output

def strToBool(str):
    return str.lower() in ("yes", "true", "t", "1", "on")


class ShotgunDeadlineRenderTask(ShotgunHieroObjectBase, hiero.core.TaskBase):
    def __init__(self, jobType, initDict, scriptPath, tempPath, settings):
        hiero.core.TaskBase.__init__(self, initDict)
        # Set the submission settings.
        self.tempPath = tempPath
        self.settings = settings
        self.initDict = initDict
        
        # Set the script path.
        self.scriptPath = scriptPath
        
        # Figure out the job name and batch name
        self.jobName = os.path.splitext(os.path.basename(scriptPath))[0]
        self.batchname = self.settings.value("BatchName")

        fwdead = self.app.frameworks['tk-framework-deadline']
        self.deadlineApiCon = fwdead.deadline_connection() 

        if not self.deadlineApiCon:
            self.app.log_error("ERROR: Could not connect to deadline")
            return
        
        fw = self.app.frameworks['tk-framework-nozon']
        self.csp = fw.import_module("colorspace")

    def startTask(self):

        resolved_export_path = self.resolvedExportPath()
        # convert slashes to native os style..
        resolved_export_path = resolved_export_path.replace( "/", os.path.sep )

        sg_current_user = tank.util.get_current_user(self.app.tank)
        userlogin = sg_current_user['login']

        _sg_shot = self.app.execute_hook(
            "hook_get_shot",
            task=self,
            item=self._item,
            data=self.app.preprocess_data,
            fields=["code", "sg_cut_in", "sg_cut_out", "sg_sequence", "sg_sequence.Sequence.episode"],
            base_class=HieroGetShot,
        )

        ctx = self.app.tank.context_from_entity("Shot", _sg_shot["id"])
        entity_type = _sg_shot['type']
        entity_id = _sg_shot['id']
        shot_name = _sg_shot["code"]
        sequence_name = _sg_shot["sg_sequence"]["name"]
        episode_name = _sg_shot.get("sg_sequence.Sequence.episode", {}).get("name")


        # Publish information
        #####################
        tk_version = int(self._formatTkVersionString(self.versionString()))
        
        publish_info = {'name': os.path.basename(resolved_export_path),
                        'published_file_type': self.app.get_setting("plate_published_file_type"), 
                        'version_number': tk_version, 
                        'comment': "Hiero Pull",
                        }

        # call the publish data hook to allow for publish customization
        extra_publish_data = self.app.execute_hook(
            "hook_get_extra_publish_data",
            task=self,
            base_class=HieroGetExtraPublishData,
        )
        if extra_publish_data is not None:
            publish_info.update(extra_publish_data)   

        # TODO? Check for conflicting publishes before proceeding 
        # conflicting_publishes = self._get_conflicting_publishes(ctx, resolved_export_path, publish_info["name"])
        # print('conflicting_publishes : %s' % conflicting_publishes)
        # if conflicting_publishes:
        #     self.app.log_error("Error: SG Transcode job to path: %s conflicts with an existing publish" % resolved_export_path)
        #     return

        # Store the publish info in json
        publish_info = json.dumps(publish_info)


        # Task information
        sg_task = None
        try:
            task_filter = self.app.get_setting("default_task_filter", "[]")
            task_filter = ast.literal_eval(task_filter)
            task_filter.append(["entity", "is", _sg_shot])
            task_fields = ["step", "content", "name"]
            tasks = self.app.shotgun.find("Task", filters=task_filter, fields=task_fields)
            if len(tasks) == 1:
                sg_task = tasks[0]
        except ValueError:
            # continue without task
            self.app.log_error("Invalid value for 'default_task_filter': %s. No task found." % setting)

        task_id = "NoTask"
        step_name = "Editorial"
        if sg_task:
            ctx_dict = ctx.to_dict()
            ctx_dict["step"] = sg_task['step']
            ctx_dict["task"] = sg_task
            ctx = sgtk.Context.from_dict(self.app.sgtk, ctx_dict)
            task_id = sg_task.get("id")
            step_name = sg_task['step']['name']


        # Get info from fields of export filepath
        tk = self.app.sgtk
        tmpl = tk.template_from_path(self.resolvedExportPath())
        if not tmpl:
            self.app.log_info("ERROR: The path: %s cannot be translated to a ShotGrid template. Please check that the export preset path is correct." % self.resolvedExportPath()) 
            return

        fields = tmpl.get_fields(self.resolvedExportPath())
        output_type = fields['output']
        colorspace = fields['colorspace']

        framerate = None
        if self._sequence:
            framerate = self._sequence.framerate()
        if self._clip.framerate().isValid(): # Note that frame rate is taken from clip framerate, not from the sequence frame rate....
            framerate = self._clip.framerate()

        # if distributed query shotgun for config path
        pc_path = self.app.sgtk.pipeline_configuration.get_path()
        
        if tk.pipeline_configuration.is_auto_path():
            filters = [['project', 'is', {'type': 'Project', 'id': self.app.context.project['id']}]]
            shotgun_fields = ["windows_path", "code"]
            data = tk.shotgun.find('PipelineConfiguration', filters=filters, fields=shotgun_fields)
            
            for pc in data:
                if pc['code'] == "Primary" and pc['windows_path']:
                    pc_path = os.path.normpath(pc['windows_path'])
                    break

        project_directory = tk.pipeline_configuration.get_project_disk_name()

        
        # Figure out the start and end frames.
        startFrame = 0
        endFrame = 0
        if isinstance(self._item, Sequence):
            startFrame = self._sequence.inTime()
            endFrame = self._sequence.outTime()
        if isinstance(self._item, Clip):
            try:
                startFrame = initDict['startFrame']
                endFrame = initDict['endFrame']
            except:
                startFrame, endFrame = self.outputRange(ignoreRetimes=True, clampToSource=False)
        if isinstance(self._item, TrackItem):
            # startFrame, endFrame = self.outputRange(ignoreRetimes=True, clampToSource=False) # this is not correct for retimed track items
            startFrame = self.initDict['startFrame']
            endFrame = self.initDict['endFrame']
        
        # Build the frame list from the start and end frames.
        frameList = str(startFrame)
        if startFrame != endFrame:
            frameList = frameList + "-" + str(endFrame)
            
        # Figure out the output path.
        outputPath = self.resolvedExportPath()
        outputPath = os.path.normpath(outputPath)
        
        # Figure out the chunksize.
        chunkSize = self.settings.value("FramesPerTask")
        if hiero.core.isVideoFileExtension(os.path.splitext(outputPath)[1].lower()):
            chunkSize = endFrame - startFrame + 1


        job_opt_ins = []
        job_opt_ins.append("NozMov2EventPlugin")
        # Opt-in to the CreateFirstCompOutputEvent only if it has been selected in the submission settings
        # and if the plate type and template are right. Add copyFirstCompToLatest if necessary
        copyFirstCompToLatest = "false"

        if strToBool(self.settings.value("CreateFirstCompOutput")):
            if output_type == self.app.get_setting("first_comp_output_plate_filter"):
                if tmpl.name in self.app.get_setting("first_comp_output_template_filter"):
                    job_opt_ins.append("PlateToFirstCompOutputEvent")
                    # add the copytolatest if it was selected
                    if strToBool(self.settings.value("CopyLatest")):
                        copyFirstCompToLatest = "true"

        # Calculate output colorspace for the firstCompOutput
        first_comp_colorspace = ""
        first_comp_colorspace = self.app.get_setting("first_comp_output_colorspace")

        if first_comp_colorspace.lower() == 'camera':
            if isinstance(self._item, TrackItem):
                read_node = self._item.source().readNode()
                first_comp_colorspace = self.csp.ColorSpace().get_read_colorspace_name(read_node)


        #NovMov app
        nozmov_app = self.app.engine.apps.get("tk-multi-nozmov")
        if not nozmov_app:
            self.app.log_error("Error : tk-multi-nozmov app not found or could not initialise")
            raise Exception

        # need to calc the output path of the nozmov movie
        nozmov_preset_name = self.app.get_setting("noz_movie_settings_preset")
        nozmov_path = nozmov_app.calc_output_filepath(outputPath, nozmov_preset_name)

        # create preset settings - could be more than one
        nozmov = [{"preset_name":nozmov_preset_name, "path": nozmov_path,
                    "first_frame": startFrame, "last_frame": endFrame, "upload": True, "add_audio": False}]
        nozmovs = {"NozMov0": nozmov}
        nozmovs = json.dumps(nozmovs)


        self.app.log_info( "==============================================================" )
        self.app.log_info( "Preparing job for deadline submission: " + self.jobName )
        self.app.log_info( "Script path: " + self.scriptPath )
        self.app.log_info( "Frame list: " + frameList )
        self.app.log_info( "Output path: " + outputPath )
        

        # Construct the job info dict
        JobInfo = {
            "Plugin": "Nuke",
            "Name" : self.jobName,
            "BatchName" : "NukeStudio - %s - %s" % (self.app.context.project['name'], self.batchname),
            "Comment" : self.settings.value("Comment"),
            "Department" : self.settings.value("Department"),
            "Pool" : self.settings.value("Pool"),
            "SecondaryPool" : self.settings.value("SecondaryPool"),
            "Group" : self.settings.value("Group"),
            "Priority" : self.settings.value("Priority"),
            "MachineLimit" : self.settings.value("MachineLimit"),
            "TaskTimeoutMinutes" : self.settings.value("TaskTimeout"),
            "EnableAutoTimeout" : self.settings.value("AutoTaskTimeout"),
            "ConcurrentTasks" : self.settings.value("ConcurrentTasks"),
            "LimitConcurrentTasksToNumberOfCpus" : self.settings.value("LimitConcurrentTasks"),
            "LimitGroups=" : self.settings.value("Limits"),
            "OnJobComplete" :  self.settings.value("OnJobComplete"),
            "EventOptIns": ",".join(job_opt_ins),
            "Frames" : frameList,
            "ChunkSize" : chunkSize,
            "OutputFilename0" : os.path.basename(outputPath),
            "OutputDirectory0" : os.path.dirname(outputPath),
            "MachineName": platform.node(),
            "UserName": userlogin,
            "ExtraInfo0": step_name,
            "ExtraInfo1": self.app.context.project['name'],
            "ExtraInfo2": "%s" % shot_name,
            "ExtraInfo3": "%s %s v%03d" % (shot_name, output_type, tk_version),
            "ExtraInfo4": "Pull",
            "ExtraInfo5": userlogin,
            "EnvironmentKeyValue0": "NOZ_TK_CONFIG_PATH=%s" % pc_path,
            "ExtraInfoKeyValue0": "UserName=%s" %  userlogin,
            "ExtraInfoKeyValue1": "Description=Pull",
            "ExtraInfoKeyValue2": "ProjectName=%s" % self.app.context.project['name'], 
            "ExtraInfoKeyValue3": "EntityName=%s" % shot_name,
            "ExtraInfoKeyValue4": "TaskName=%s" % step_name,
            "ExtraInfoKeyValue5": "EntityType=%s" % entity_type,
            "ExtraInfoKeyValue6": "ProjectId=%i" % self.app.context.project['id'],
            "ExtraInfoKeyValue7": "EntityId=%i" % entity_id,
            "ExtraInfoKeyValue8": "TaskId=%s" % task_id,
            "ExtraInfoKeyValue9": "ProjectDirectory=%s" % project_directory,
            "ExtraInfoKeyValue10": "context=%s" % ctx.serialize(with_user_credentials=False, use_json=True),
            "ExtraInfoKeyValue11": "PublishInfo=%s" % publish_info,
            "ExtraInfoKeyValue12": "ProjectScriptFolder=%s" % os.path.join(pc_path, "config", "hooks", "tk-multi-publish2", "nozonpub"),
            "ExtraInfoKeyValue13": "FrameRate=%s" % str(framerate),
            "ExtraInfoKeyValue14": 'Colorspaces={"Colorspace0": "%s"}' % colorspace,
            "ExtraInfoKeyValue15": 'copyFirstCompToLatest=%s' % copyFirstCompToLatest,
            "ExtraInfoKeyValue16": 'FirstCompOutputColorspace=%s' % first_comp_colorspace,
            "ExtraInfoKeyValue17": "NozMovDeadlineEventScript=%s" % nozmov_app.get_setting("deadline_event_script"),
            "ExtraInfoKeyValue18": "NozMovDeadlinePluginScript=%s" % nozmov_app.get_setting("deadline_plugin_script"),
            "ExtraInfoKeyValue19": "NozMovs=%s" % nozmovs,
            }

        if strToBool(self.settings.value("SubmitSuspended")):
            JobInfo["InitialStatus"] = "Suspended"

        if strToBool(self.settings.value("IsBlacklist")):
            JobInfo["Blacklist"] = self.settings.value("MachineList")
        else:
            JobInfo["Whitelist"] = self.settings.value("MachineList")


        # Constuct the plugin info dict
        PluginInfo = {
            "Version" : self.settings.value("Version"),
            "Threads" : self.settings.value("Threads"),
            "RamUse" : self.settings.value("Memory"),
            "Build" : self.settings.value("Build"),
            "BatchMode" : self.settings.value("BatchMode"),
            "NukeX" : self.settings.value("UseNukeX"),
            "ContinueOnError" : self.settings.value("ContinueOnError"),
            "UseGpu":False,
            "VerbosityLevel": 2,
            }

        if not strToBool(self.settings.value("SubmitScript")):
            PluginInfo["SceneFile"] = self.scriptPath


        # Submit job to deadline using the deadline API
        job = self.deadlineApiCon.Jobs.SubmitJob(JobInfo, PluginInfo)

        if sg_task:
            self.app.shotgun.update("Task", sg_task["id"], {'sg_status_list':'ip'})

        return job["_id"]


    def _get_conflicting_publishes(self, context, path, publish_name, filters=None):
        """
        Nozon : code taken from multi-publish2

        Returns a list of SG published file dicts for any existing publishes that
        match the supplied context, path, and publish_name.

        :param context: The context to search publishes for
        :param path: The path to match against previous publishes
        :param publish_name: The name of the publish.
        :param filters: A list of additional SG find() filters to apply to the
            publish search.

        :return: A list of ``dict``s representing existing publishes that match
            the supplied arguments. The paths returned are the standard "id", and
            "type" as well as the "path" field.

        This method is typically used by publish plugin hooks to determine if there
        are existing publishes for a given context, publish_name, and path and
        warning appropriately.
        """

        # ask core to do a dry_run of a publish with the supplied criteria. this is
        # a workaround for our inability to filter publishes by path. so for now,
        # get a dictionary of data that would be used to create a matching publish
        # and use that to get publishes via a call to find(). Then we'll filter
        # those by their path field. Once we have the ability in SG to filter by
        # path, we can replace this whole method with a simple call to find().
        publish_data = sgtk.util.register_publish(self.app.sgtk, context, path, publish_name, version_number=None, dry_run=True)

        # now build up the filters to match against
        publish_filters = [filters] if filters else []
        for field in ["code", "entity", "name", "project", "task"]:
            publish_filters.append([field, "is", publish_data[field]])

        # run the
        publishes = self.app.sgtk.shotgun.find("PublishedFile", publish_filters, ["path"])

        # ensure the path is normalized for comparison
        normalized_path = sgtk.util.ShotgunPath.normalize(path)

        # next, extract the publish path from each of the returned publishes and
        # compare it against the supplied path. if the paths match, we add the
        # publish to the list of publishes to return.
        matching_publishes = []
        for publish in publishes:
            publish_path = sgtk.util.resolve_publish_path(self.app.sgtk, publish)
            if publish_path:
                # ensure the published path is normalized for comparison
                normalized_publish_path = sgtk.util.ShotgunPath.normalize(publish_path)
                if normalized_path == normalized_publish_path:
                    matching_publishes.append(publish)

        return matching_publishes






# Create a Submission and add your Task
class ShotgunDeadlineRenderSubmission(ShotgunHieroObjectBase, Submission):

    kNukeRender = "deadline_submission"

    def __init__(self):
        Submission.__init__(self)
        self.lastSelection = ""
        self.jobId = None

    def initialise(self):
        self.settingsFile = os.path.join(self.findNukeHomeDir(), "deadline_settings.ini")
        self.app.log_debug( "Loading deadline ini settings: " + self.settingsFile )
        
        # Initialize the submission settings.
        self.settings = QSettings(self.settingsFile, QSettings.IniFormat)
        
        # Get the Deadline temp directroy.
        deadlineHome = CallDeadlineCommand( ["-GetCurrentUserHomeDirectory",] )
        deadlineHome = deadlineHome.decode()
        deadlineHome = deadlineHome.replace( "\n", "" ).replace( "\r", "" )
        self.deadlineTemp = deadlineHome + "/temp"
        
        # Get maximum priority.
        maximumPriority = 100
        try:
            output = CallDeadlineCommand( ["-getmaximumpriority",] )
            maximumPriority = int(output)
        except:
            # If an error occurs here, just ignore it and use the default of 100.
            pass
        
        # Collect the pools and groups.
        output = CallDeadlineCommand( ["-pools",] )
        output = output.decode()
        pools = output.splitlines()
        
        secondaryPools = []
        secondaryPools.append("")
        for currPool in pools:
            secondaryPools.append(currPool)
        
        output = CallDeadlineCommand( ["-groups",] )
        output = output.decode()
        groups = output.splitlines()

        # Set up the other default arrays.
        onJobComplete = ("Nothing","Archive","Delete")
        nukeVersions = ("11.0","11.1", "11.2", "11.3", "12.1", "12.2", "13.0", "13.1", "13.2", "14.0", "14.1", "15.0")
        running_nukestudio_version = "%s.%s" % (hiero.core.env["VersionMajor"], hiero.core.env["VersionMinor"])
        buildsToForce = ("None","32bit","64bit")
        
        # Main Window
        mainWindow = hiero.ui.mainWindow()
        dialog = QDialog(mainWindow)
        self.dialog = dialog
        dialog.setWindowTitle("Submit to Deadline (and render with Nuke)")
        
        # Main Layout
        topLayout = QVBoxLayout()
        dialog.setLayout(topLayout)
        tabWidget = QTabWidget(dialog)
        
        jobTab = QWidget()
        jobTabLayout = QVBoxLayout()
        jobTab.setLayout(jobTabLayout)
        
        # Job Info Layout
        jobInfoGroupBox = QGroupBox("Job Description")
        jobTabLayout.addWidget(jobInfoGroupBox)
        jobInfoLayout = QGridLayout()
        jobInfoGroupBox.setLayout(jobInfoLayout)
        
        # Job Name
        jobInfoLayout.addWidget(QLabel("Batch Name"), 0, 0)
        batchNameWidget = QLineEdit(self.settings.value("BatchName", ""))
        jobInfoLayout.addWidget(batchNameWidget, 0, 1)
        
        # Comment
        jobInfoLayout.addWidget(QLabel("Comment"), 1, 0)
        commentWidget = QLineEdit(self.settings.value("Comment", ""))
        jobInfoLayout.addWidget(commentWidget, 1, 1)
        
        # Department
        jobInfoLayout.addWidget(QLabel("Department"), 2, 0)
        departmentWidget = QLineEdit(self.settings.value("Department", ""))
        jobInfoLayout.addWidget(departmentWidget, 2, 1)
        
        
        # Job Options Layout
        jobOptionsGroupBox = QGroupBox("Job Options")
        jobTabLayout.addWidget(jobOptionsGroupBox)
        jobOptionsLayout = QGridLayout()
        jobOptionsGroupBox.setLayout(jobOptionsLayout)
        
        # Pool
        jobOptionsLayout.addWidget(QLabel("Pool"), 0, 0)
        poolWidget = QComboBox()
        for pool in pools:
            poolWidget.addItem(pool)
        
        defaultPool = self.settings.value("Pool", "none")
        defaultIndex = poolWidget.findText(defaultPool)
        if defaultIndex != -1:
            poolWidget.setCurrentIndex(defaultIndex)
            
        jobOptionsLayout.addWidget(poolWidget, 0, 1, 1, 3)
        
        # Secondary Pool
        jobOptionsLayout.addWidget(QLabel("Secondary Pool"), 1, 0)
        secondaryPoolWidget = QComboBox()
        for secondaryPool in secondaryPools:
            secondaryPoolWidget.addItem(secondaryPool)
        
        defaultSecondaryPool = self.settings.value("SecondaryPool", "")
        defaultIndex = secondaryPoolWidget.findText(defaultSecondaryPool)
        if defaultIndex != -1:
            secondaryPoolWidget.setCurrentIndex(defaultIndex)
            
        jobOptionsLayout.addWidget(secondaryPoolWidget, 1, 1, 1, 3)
        
        # Group
        jobOptionsLayout.addWidget(QLabel("Group"), 2, 0)
        groupWidget = QComboBox()
        for group in groups:
            groupWidget.addItem(group)
            
        defaultGroup = self.settings.value("Group", "none")
        defaultIndex = groupWidget.findText(defaultGroup)
        if defaultIndex != -1:
            groupWidget.setCurrentIndex(defaultIndex)
            
        jobOptionsLayout.addWidget(groupWidget, 2, 1, 1, 3)
        
        # Priority
        initPriority = int(self.settings.value("Priority", "50"))
        if initPriority > maximumPriority:
            initPriority = maximumPriority / 2
        
        jobOptionsLayout.addWidget(QLabel("Priority"), 3, 0)
        priorityWidget = QSpinBox()
        priorityWidget.setRange(0, maximumPriority)
        priorityWidget.setValue(initPriority)
        jobOptionsLayout.addWidget(priorityWidget, 3, 1)
        
        # Task Timeout
        jobOptionsLayout.addWidget(QLabel("Task Timeout"), 4, 0)
        taskTimeoutWidget = QSpinBox()
        taskTimeoutWidget.setRange(0, 1000000)
        taskTimeoutWidget.setValue(int(self.settings.value("TaskTimeout", "0")))
        jobOptionsLayout.addWidget(taskTimeoutWidget, 4, 1)
        
        # Auto Task Timeout
        autoTaskTimeoutWidget = QCheckBox("Enable Auto Task Timeout")
        autoTaskTimeoutWidget.setChecked(strToBool(self.settings.value("AutoTaskTimeout", "False")))
        jobOptionsLayout.addWidget(autoTaskTimeoutWidget, 4, 2)
        
        # Concurrent Tasks
        jobOptionsLayout.addWidget(QLabel("Concurrent Tasks"), 5, 0)
        concurrentTasksWidget = QSpinBox()
        concurrentTasksWidget.setRange(1, 16)
        concurrentTasksWidget.setValue(int(self.settings.value("ConcurrentTasks", "1")))
        jobOptionsLayout.addWidget(concurrentTasksWidget, 5, 1)
        
        # Limit Tasks To Slave's Task Limit
        limitConcurrentTasksWidget = QCheckBox("Limit Tasks To Slave's Task Limit")
        limitConcurrentTasksWidget.setChecked(strToBool(self.settings.value("LimitConcurrentTasks", "True")))
        jobOptionsLayout.addWidget(limitConcurrentTasksWidget, 5, 2)
        
        # Machine Limit
        jobOptionsLayout.addWidget(QLabel("Machine Limit"), 6, 0)
        machineLimitWidget = QSpinBox()
        machineLimitWidget.setRange(0, 1000000)
        machineLimitWidget.setValue(int(self.settings.value("MachineLimit", "1")))
        jobOptionsLayout.addWidget(machineLimitWidget, 6, 1)
        
        # Machine List Is A Blacklist
        isBlacklistWidget = QCheckBox("Machine List Is A Blacklist")
        isBlacklistWidget.setChecked(strToBool(self.settings.value("IsBlacklist", "False")))
        jobOptionsLayout.addWidget(isBlacklistWidget, 6, 2)
        
        # Machine List
        jobOptionsLayout.addWidget(QLabel("Machine List"), 7, 0)
        machineListWidget = QLineEdit(self.settings.value("MachineList", ""))
        jobOptionsLayout.addWidget(machineListWidget, 7, 1, 1, 2)
        
        def browseMachineList():
            output = CallDeadlineCommand(["-selectmachinelist", str(machineListWidget.text())], False)
            output = output.decode()
            output = output.replace("\r", "").replace("\n", "")
            if output != "Action was cancelled by user":
                machineListWidget.setText(output)
        
        machineListButton = QPushButton("Browse")
        machineListButton.pressed.connect(browseMachineList)
        jobOptionsLayout.addWidget(machineListButton, 7, 3)
        
        # Limits
        jobOptionsLayout.addWidget(QLabel("Limits"), 8, 0)
        limitsWidget = QLineEdit(self.settings.value("Limits", ""))
        jobOptionsLayout.addWidget(limitsWidget, 8, 1, 1, 2)
        
        def browseLimitList():
            output = CallDeadlineCommand(["-selectlimitgroups", str(limitsWidget.text())], False)
            output = output.replace("\r", "").replace("\n", "")
            if output != "Action was cancelled by user":
                limitsWidget.setText(output)
        
        limitsButton = QPushButton("Browse")
        limitsButton.pressed.connect(browseLimitList)
        jobOptionsLayout.addWidget(limitsButton, 8, 3)
        
        # On Job Complete
        jobOptionsLayout.addWidget(QLabel("On Job Complete"), 9, 0)
        onJobCompleteWidget = QComboBox()
        for option in onJobComplete:
            onJobCompleteWidget.addItem(option)
            
        defaultOption = self.settings.value("OnJobComplete", "Nothing")
        defaultIndex = onJobCompleteWidget.findText(defaultOption)
        if defaultIndex != -1:
            onJobCompleteWidget.setCurrentIndex(defaultIndex)
            
        jobOptionsLayout.addWidget(onJobCompleteWidget, 9, 1)
        
        # Submit Job As Suspended
        submitSuspendedWidget = QCheckBox("Submit Job As Suspended")
        submitSuspendedWidget.setChecked(strToBool(self.settings.value("SubmitSuspended", "False")))
        jobOptionsLayout.addWidget(submitSuspendedWidget, 9, 2)
        

        # Create First Comp output Nozon
        CreateFirstCompOutputWidget = QCheckBox("Create first comp output version")
        CreateFirstCompOutputWidget.setChecked(strToBool(self.settings.value("CreateFirstCompOutput", "True")))
        jobOptionsLayout.addWidget(CreateFirstCompOutputWidget, 10, 0)


        # Copy to latest of first comp output
        CopyLatestWidget = QCheckBox("Copy to latest of first comp output")
        CopyLatestWidget.setChecked(strToBool(self.settings.value("CopyLatest", "True")))
        jobOptionsLayout.addWidget(CopyLatestWidget, 10, 1)


        # Nuke Options
        nukeOptionsGroupBox = QGroupBox("Nuke Options")
        jobTabLayout.addWidget(nukeOptionsGroupBox)
        nukeOptionsLayout = QGridLayout()
        nukeOptionsGroupBox.setLayout(nukeOptionsLayout)
        
        # Version
        nukeOptionsLayout.addWidget(QLabel("Version"), 0, 0)
        versionWidget = QComboBox()
        for version in nukeVersions:
            versionWidget.addItem(version)
            
        defaultVersion = self.settings.value("Version", running_nukestudio_version)
        defaultIndex = versionWidget.findText(defaultVersion)
        if defaultIndex != -1:
            versionWidget.setCurrentIndex(defaultIndex)
            
        nukeOptionsLayout.addWidget(versionWidget, 0, 1)
        
        # Submit Nuke Script File With Job
        submitScriptWidget = QCheckBox("Submit Nuke Script File With Job")
        submitScriptWidget.setChecked(strToBool(self.settings.value("SubmitScript", "False")))
        nukeOptionsLayout.addWidget(submitScriptWidget, 0, 2)
        
        # Build To Force
        nukeOptionsLayout.addWidget(QLabel("Build To Force"), 1, 0)
        buildWidget = QComboBox()
        for build in buildsToForce:
            buildWidget.addItem(build)
            
        defaultBuild = self.settings.value("Build", "None")
        defaultIndex = buildWidget.findText(defaultBuild)
        if defaultIndex != -1:
            buildWidget.setCurrentIndex(defaultIndex)
            
        nukeOptionsLayout.addWidget(buildWidget, 1, 1)
        
        # Render With NukeX
        useNukeXWidget = QCheckBox("Render With NukeX")
        useNukeXWidget.setChecked(strToBool(self.settings.value("UseNukeX", "False")))
        nukeOptionsLayout.addWidget(useNukeXWidget, 1, 2)
        
        # Max RAM Usage (MB)
        nukeOptionsLayout.addWidget(QLabel("Max RAM Usage (MB)"), 2, 0)
        memoryWidget = QSpinBox()
        memoryWidget.setRange(0, 5000)
        memoryWidget.setValue(int(self.settings.value("Memory", "0")))
        nukeOptionsLayout.addWidget(memoryWidget, 2, 1)
        
        # Continue On Error
        continueOnErrorWidget = QCheckBox("Continue On Error")
        continueOnErrorWidget.setChecked(strToBool(self.settings.value("ContinueOnError", "False")))
        nukeOptionsLayout.addWidget(continueOnErrorWidget, 2, 2)
        
        # Threads
        nukeOptionsLayout.addWidget(QLabel("Threads"), 3, 0)
        threadsWidget = QSpinBox()
        threadsWidget.setRange(0, 256)
        threadsWidget.setValue(int(self.settings.value("Threads", "0")))
        nukeOptionsLayout.addWidget(threadsWidget, 3, 1)
        
        # Use Batch Mode
        batchModeWidget = QCheckBox("Use Batch Mode")
        batchModeWidget.setChecked(strToBool(self.settings.value("BatchMode", "True")))
        nukeOptionsLayout.addWidget(batchModeWidget, 3, 2)
        
        # Frames Per Task
        nukeOptionsLayout.addWidget(QLabel("Frames Per Task"), 4, 0)
        framesPerTaskWidget = QSpinBox()
        framesPerTaskWidget.setRange(1, 1000000)
        framesPerTaskWidget.setValue(int(self.settings.value("FramesPerTask", "1")))
        nukeOptionsLayout.addWidget(framesPerTaskWidget, 4, 1)
        nukeOptionsLayout.addWidget(QLabel("(this only affects non-movie jobs)"), 4, 2)
        
        tabWidget.addTab(jobTab, "Job Options")

        submitButton = QPushButton("Submit")
        submitButton.clicked.connect( dialog.accept )
        submitButton.setDefault( True )

        cancelButton = QPushButton("Cancel")
        cancelButton.clicked.connect( dialog.reject )

        buttonGroupBox = QGroupBox()
        buttonLayout = QGridLayout()
        buttonGroupBox.setLayout(buttonLayout)
        # Push buttons over to the right, otherwise they'll spread over the entire width
        buttonGroupBox.setContentsMargins( 180, 0, 0, 0)
        buttonGroupBox.setAlignment( Qt.AlignRight )
        buttonGroupBox.setFlat( True )
        buttonLayout.addWidget(submitButton, 0, 1)
        buttonLayout.addWidget(cancelButton, 0, 2)

        topLayout.addWidget(tabWidget)
        topLayout.addWidget(buttonGroupBox)

        # Show the dialog.
        result = (dialog.exec_() == QDialog.DialogCode.Accepted)
        if result:
            self.settings.setValue("BatchName", batchNameWidget.text())
            self.settings.setValue("Comment", commentWidget.text())
            self.settings.setValue("Department", departmentWidget.text())
            self.settings.setValue("Pool", poolWidget.currentText())
            self.settings.setValue("SecondaryPool", secondaryPoolWidget.currentText())
            self.settings.setValue("Group", groupWidget.currentText())
            self.settings.setValue("Priority", priorityWidget.value())
            self.settings.setValue("TaskTimeout", taskTimeoutWidget.value())
            self.settings.setValue("AutoTaskTimeout", str(autoTaskTimeoutWidget.isChecked()))
            self.settings.setValue("ConcurrentTasks", concurrentTasksWidget.value())
            self.settings.setValue("LimitConcurrentTasks", str(limitConcurrentTasksWidget.isChecked()))
            self.settings.setValue("MachineLimit", machineLimitWidget.value())
            self.settings.setValue("IsBlacklist", str(isBlacklistWidget.isChecked()))
            self.settings.setValue("MachineList", machineListWidget.text())
            self.settings.setValue("Limits", limitsWidget.text())
            self.settings.setValue("OnJobComplete", onJobCompleteWidget.currentText())
            self.settings.setValue("SubmitSuspended", str(submitSuspendedWidget.isChecked()))
            self.settings.setValue("CreateFirstCompOutput", str(CreateFirstCompOutputWidget.isChecked()))
            self.settings.setValue("CopyLatest", str(CopyLatestWidget.isChecked()))
            self.settings.setValue("Version", versionWidget.currentText())
            self.settings.setValue("SubmitScript", str(submitScriptWidget.isChecked()))
            self.settings.setValue("Build", buildWidget.currentText())
            self.settings.setValue("UseNukeX", str(useNukeXWidget.isChecked()))
            self.settings.setValue("FramesPerTask", framesPerTaskWidget.value())
            self.settings.setValue("ContinueOnError", str(continueOnErrorWidget.isChecked()))
            self.settings.setValue("Threads", threadsWidget.value())
            self.settings.setValue("BatchMode", str(batchModeWidget.isChecked()))
            self.settings.setValue("Memory", memoryWidget.value())
            
            self.app.log_debug( "Saving settings: " + self.settingsFile )
            self.settings.sync()
        else:
            self.app.log_info( "Submission canceled" )
            self.settings = None
            # Not sure if there is a better way to stop the export process. This works, but it leaves all the tasks
            # in the Queued state.
            self.setError( "Submission was canceled" )
    
    def addJob(self, jobType, initDict, filePath):
        # Only create a task if submission wasn't canceled.
        if self.settings != None:
            self.jobId = ShotgunDeadlineRenderTask( Submission.kCommandLine, initDict, filePath, self.deadlineTemp, self.settings )
            return self.jobId

        
    def findNukeHomeDir(self):
        return os.path.normpath(os.path.join(hiero.core.env["HomeDirectory"], ".nuke"))



