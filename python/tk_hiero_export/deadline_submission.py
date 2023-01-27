import os
import sys
import re
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




IntegrationKVPs = {}

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

def OpenIntegrationWindow():
    global IntegrationKVPs

    scriptPath = CallDeadlineCommand( ["-getrepositoryfilepath", "submission/Integration/Main/IntegrationUIStandAlone.py"], False )
    scriptPath = scriptPath.decode()
    scriptPath = scriptPath.strip()

    argArray = ["-ExecuteScript", scriptPath, "Hiero", "Draft", "NIM", "0"]

    results = CallDeadlineCommand( argArray, False )
    keyValuePairs = {}

    outputLines = results.splitlines()

    for line in outputLines:
        line = line.strip()
        if not line.startswith("("):
            tokens = line.split( "=", 1 )

            if len( tokens ) > 1:
                key = tokens[0]
                value = tokens [1]

                keyValuePairs[key] = value

    if len( keyValuePairs ) > 0:
        IntegrationKVPs = keyValuePairs

class DeadlineRenderTask(hiero.core.TaskBase):
    def __init__(self, jobType, initDict, scriptPath, tempPath, settings):
        hiero.core.TaskBase.__init__(self, initDict)
        # Set the submission settings.
        self.tempPath = tempPath
        self.settings = settings
        
        # print("XXXXXXXXXX inside DeadlineRenderTask initDict %s" % initDict)

        # Set the script path.
        self.scriptPath = scriptPath
        
        # Figure out the job name from the script file.
        self.jobName = os.path.splitext(os.path.basename(scriptPath))[0]
        tempJobName = self.settings.value("JobName")
        if tempJobName != "":
            self.jobName = tempJobName + " - " + self.jobName
        
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
            startFrame = initDict['startFrame']
            endFrame = initDict['endFrame']
        
        # Build the frame list from the start and end frames.
        self.frameList = str(startFrame)
        if startFrame != endFrame:
            self.frameList = self.frameList + "-" + str(endFrame)
            
        # Figure out the output path.
        self.outputPath = self.resolvedExportPath()
        
        # Figure out the chunksize.
        self.chunkSize = self.settings.value("FramesPerTask")
        if hiero.core.isVideoFileExtension(os.path.splitext(self.outputPath)[1].lower()):
            self.chunkSize = endFrame - startFrame + 1

    def startTask(self):
        global IntegrationKVPs

        print( "==============================================================" )
        print( "Preparing job for submission: " + self.jobName )
        print( "Script path: " + self.scriptPath )
        print( "Frame list: " + self.frameList )
        print( "Chunk size: " + str(self.chunkSize) )
        print( "Output path: " + self.outputPath )
        
        # Create the job info file.
        jobInfoFile = self.tempPath + "/hiero_submit_info.job"
        fileHandle = open( jobInfoFile, "w" )
        fileHandle.write( "Plugin=Nuke\n" )
        fileHandle.write( "Name=%s\n" % self.jobName )
        fileHandle.write( "Comment=%s\n" % self.settings.value("Comment") )
        fileHandle.write( "Department=%s\n" % self.settings.value("Department") )
        fileHandle.write( "Pool=%s\n" % self.settings.value("Pool") )
        fileHandle.write( "SecondaryPool=%s\n" % self.settings.value("SecondaryPool") )
        fileHandle.write( "Group=%s\n" % self.settings.value("Group") )
        fileHandle.write( "Priority=%s\n" % self.settings.value("Priority") )
        fileHandle.write( "MachineLimit=%s\n" % self.settings.value("MachineLimit") )
        fileHandle.write( "TaskTimeoutMinutes=%s\n" % self.settings.value("TaskTimeout") )
        fileHandle.write( "EnableAutoTimeout=%s\n" % self.settings.value("AutoTaskTimeout") )
        fileHandle.write( "ConcurrentTasks=%s\n" % self.settings.value("ConcurrentTasks") )
        fileHandle.write( "LimitConcurrentTasksToNumberOfCpus=%s\n" % self.settings.value("LimitConcurrentTasks") )
        fileHandle.write( "LimitGroups=%s\n" % self.settings.value("Limits") )
        fileHandle.write( "OnJobComplete=%s\n" % self.settings.value("OnJobComplete") )
        fileHandle.write( "Frames=%s\n" % self.frameList )
        fileHandle.write( "ChunkSize=%s\n" % self.chunkSize )
        fileHandle.write( "OutputFilename0=%s\n" % self.outputPath )
        
        if strToBool(self.settings.value("SubmitSuspended")):
            fileHandle.write( "InitialStatus=Suspended\n" )
            
        if strToBool(self.settings.value("IsBlacklist")):
            fileHandle.write( "Blacklist=%s\n" % self.settings.value("MachineList") )
        else:
            fileHandle.write( "Whitelist=%s\n" % self.settings.value("MachineList") )
        
        groupBatch = False
        if 'integrationSettingsPath' in IntegrationKVPs:
            with open( IntegrationKVPs['integrationSettingsPath'] ) as file:
                for line in file.readlines():
                    fileHandle.write( line )
            
            if 'batchMode' in IntegrationKVPs:
                if IntegrationKVPs['batchMode'] == "True":
                    groupBatch = True

        if groupBatch:
            fileHandle.write( "BatchName=%s\n" % self.jobName ) 

        fileHandle.close()
        
        # Create the plugin info file.
        pluginInfoFile = self.tempPath +"/hiero_plugin_info.job"
        fileHandle = open( pluginInfoFile, "w" )
        if not strToBool(self.settings.value("SubmitScript")):
            fileHandle.write( "SceneFile=%s\n" % self.scriptPath )
        fileHandle.write( "Version=%s\n" % self.settings.value("Version") )
        fileHandle.write( "Threads=%s\n" % self.settings.value("Threads") )
        fileHandle.write( "RamUse=%s\n" % self.settings.value("Memory") )
        fileHandle.write( "Build=%s\n" % self.settings.value("Build") )
        fileHandle.write( "BatchMode=%s\n" % self.settings.value("BatchMode") )
        fileHandle.write( "NukeX=%s\n" % self.settings.value("UseNukeX") )
        fileHandle.write( "ContinueOnError=%s\n" % self.settings.value("ContinueOnError") )
        
        fileHandle.close()
        
        # Submit the job to Deadline
        args = []
        args.append( jobInfoFile )
        args.append( pluginInfoFile )
        if strToBool(self.settings.value("SubmitScript")):
            args.append( self.scriptPath )
        
        results = CallDeadlineCommand( args )
        print( results )
        print( "Job submission complete: " + self.jobName  )

# Create a Submission and add your Task
class DeadlineRenderSubmission(Submission):
    def __init__(self):
        Submission.__init__(self)
        self.lastSelection = ""

    def initialise(self):
        self.settingsFile = os.path.join(self.findNukeHomeDir(), "deadline_settings.ini")
        print( "Loading settings: " + self.settingsFile )
        
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
        nukeVersions = ("6.0","6.1","6.2","6.3","6.4","7.0","7.1","7.2","7.3","7.4","8.0","8.1","8.2","8.3","8.4","9.0","9.1","9.2","9.3","9.4","10.0","10.1","10.2","10.3","10.4","11.0","11.1", "11.2", "11.3", "12.1", "12.2", "13.0", "13.1", "13.2")
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
        jobInfoLayout.addWidget(QLabel("Job Name"), 0, 0)
        jobNameWidget = QLineEdit(self.settings.value("JobName", ""))
        jobInfoLayout.addWidget(jobNameWidget, 0, 1)
        
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
            
        defaultVersion = self.settings.value("Version", "7.0")
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
        batchModeWidget.setChecked(strToBool(self.settings.value("BatchMode", "False")))
        nukeOptionsLayout.addWidget(batchModeWidget, 3, 2)
        
        # Frames Per Task
        nukeOptionsLayout.addWidget(QLabel("Frames Per Task"), 4, 0)
        framesPerTaskWidget = QSpinBox()
        framesPerTaskWidget.setRange(1, 1000000)
        framesPerTaskWidget.setValue(int(self.settings.value("FramesPerTask", "1")))
        nukeOptionsLayout.addWidget(framesPerTaskWidget, 4, 1)
        nukeOptionsLayout.addWidget(QLabel("(this only affects non-movie jobs)"), 4, 2)
        
        tabWidget.addTab(jobTab, "Job Options")

        # Button Box (Extra work required to get the custom ordering we want)
        integrationButton = QPushButton("Pipeline Tools")
        integrationButton.clicked.connect( OpenIntegrationWindow )

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
        buttonLayout.addWidget(integrationButton, 0, 1)
        buttonLayout.addWidget(submitButton, 0, 2)
        buttonLayout.addWidget(cancelButton, 0, 3)

        topLayout.addWidget(tabWidget)
        topLayout.addWidget(buttonGroupBox)

        # Show the dialog.
        result = (dialog.exec_() == QDialog.DialogCode.Accepted)
        if result:
            self.settings.setValue("JobName", jobNameWidget.text())
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
            self.settings.setValue("Version", versionWidget.currentText())
            self.settings.setValue("SubmitScript", str(submitScriptWidget.isChecked()))
            self.settings.setValue("Build", buildWidget.currentText())
            self.settings.setValue("UseNukeX", str(useNukeXWidget.isChecked()))
            self.settings.setValue("FramesPerTask", framesPerTaskWidget.value())
            self.settings.setValue("ContinueOnError", str(continueOnErrorWidget.isChecked()))
            self.settings.setValue("Threads", threadsWidget.value())
            self.settings.setValue("BatchMode", str(batchModeWidget.isChecked()))
            self.settings.setValue("Memory", memoryWidget.value())
            
            print( "Saving settings: " + self.settingsFile )
            self.settings.sync()
        else:
            print( "Submission canceled" )
            self.settings = None
            # Not sure if there is a better way to stop the export process. This works, but it leaves all the tasks
            # in the Queued state.
            self.setError( "Submission was canceled" )
    
    def addJob(self, jobType, initDict, filePath):
        # Only create a task if submission wasn't canceled.
        if self.settings != None:
            print("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
            return DeadlineRenderTask( Submission.kCommandLine, initDict, filePath, self.deadlineTemp, self.settings )
        
    def findNukeHomeDir(self):
        return os.path.normpath(os.path.join(hiero.core.env["HomeDirectory"], ".nuke"))









#### Add this custom deadline submitter
# hiero.core.taskRegistry.addSubmission( "ZZZ Submit to Deadline", DeadlineRenderSubmission )