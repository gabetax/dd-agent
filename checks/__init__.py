'''
    Datadog agent

    Licensed under Simplified BSD License (see LICENSE)
    (C) Boxed Ice 2010 all rights reserved
    (C) Datadog, Inc 2010 All Rights Reserved
'''

# SO references
# http://stackoverflow.com/questions/446209/possible-values-from-sys-platform/446210#446210
# http://stackoverflow.com/questions/682446/splitting-out-the-output-of-ps-using-python/682464#682464
# http://stackoverflow.com/questions/1052589/how-can-i-parse-the-output-of-proc-net-dev-into-keyvalue-pairs-per-interface-us

# Core modules
import httplib # Used only for handling httplib.HTTPException (case #26701)
import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys
import urllib
import urllib2
import time
import datetime

# Needed to identify server uniquely
import uuid
try:
    from hashlib import md5
except ImportError: # Python < 2.5
    from md5 import new as md5

from checks.nagios import Nagios
from checks.build import Hudson
from checks.db import CouchDb, MongoDb, MySql
from checks.queue import RabbitMq
from checks.system import Disk, IO, Load, Memory, Network, Processes, Cpu
from checks.web import Apache, Nginx
from checks.ganglia import Ganglia
from checks.datadog import RollupLP as ddRollupLP
from checks.cassandra import Cassandra

from resources.processes import Processes as ResProcesses
from resources.mockup_rails import RailsMockup

def recordsize(func):
    def wrapper(*args, **kwargs):
        logger = logging.getLogger("checks")
        res = func(*args, **kwargs)
        logger.debug("SIZE: {0} wrote {1} bytes uncompressed".format(func, len(str(res))))
        return res
    return wrapper

class checks:
    
    def __init__(self, agentConfig, rawConfig, emitter):
        self.agentConfig = agentConfig
        self.rawConfig = rawConfig
        self.plugins = None
        self.emitter = emitter
        
        macV = None
        if sys.platform == 'darwin':
            macV = platform.mac_ver()
        
        # Output from top is slightly modified on OS X 10.6 (case #28239)
        if macV and macV[0].startswith('10.6.'):
            self.topIndex = 6
        else:
            self.topIndex = 5
    
        self.os = None
        
        self.checksLogger = logging.getLogger('checks')
        # Set global timeout to 15 seconds for all sockets (case 31033). Should be long enough
        import socket
        socket.setdefaulttimeout(15)
        
        self.linuxProcFsLocation = self.getMountedLinuxProcFsLocation()
        
        self._apache = Apache()
        self._nginx = Nginx()
        self._disk = Disk()
        self._io = IO()
        self._load = Load(self.linuxProcFsLocation)
        self._memory = Memory(self.linuxProcFsLocation, self.topIndex)
        self._network = Network()
        self._processes = Processes()
        self._cpu = Cpu()
        self._couchdb = CouchDb()
        self._mongodb = MongoDb()
        self._mysql = MySql()
        self._rabbitmq = RabbitMq()
        self._ganglia = Ganglia()
        self._cassandra = Cassandra()

        if agentConfig.get('has_datadog',False):
            self._datadogs = [ddRollupLP()]
        else:
            self._datadogs = None

        self._event_checks = [Hudson(), Nagios(socket.gethostname())]
        self._resources_checks = [ResProcesses(self.checksLogger,self.agentConfig)]
 
    #
    # Checks
    #
    @recordsize 
    def getApacheStatus(self):
        return self._apache.check(self.checksLogger, self.agentConfig)

    @recordsize 
    def getCouchDBStatus(self):
        return self._couchdb.check(self.checksLogger, self.agentConfig)
    
    @recordsize
    def getDiskUsage(self):
        return self._disk.check(self.checksLogger, self.agentConfig)

    @recordsize
    def getIOStats(self):
        return self._io.check(self.checksLogger, self.agentConfig)
            
    @recordsize
    def getLoadAvrgs(self):
        return self._load.check(self.checksLogger, self.agentConfig)

    @recordsize 
    def getMemoryUsage(self):
        return self._memory.check(self.checksLogger, self.agentConfig)
        
    @recordsize
    def getVMStat(self):
        """Provide the same data that vmstat on linux provides"""
        # on mac, try top -S -n0 -l1
        pass
    
    @recordsize     
    def getMongoDBStatus(self):
        return self._mongodb.check(self.checksLogger, self.agentConfig)

    @recordsize
    def getMySQLStatus(self):
        return self._mysql.check(self.checksLogger, self.agentConfig)
        
    @recordsize
    def getNetworkTraffic(self):
        return self._network.check(self.checksLogger, self.agentConfig)
    
    @recordsize
    def getNginxStatus(self):
        return self._nginx.check(self.checksLogger, self.agentConfig)
       
    @recordsize
    def getProcesses(self):
        return self._processes.check(self.checksLogger, self.agentConfig)
 
    @recordsize
    def getRabbitMQStatus(self):
        return self._rabbitmq.check(self.checksLogger, self.agentConfig)

    @recordsize
    def getGangliaData(self):
        return self._ganglia.check(self.checksLogger, self.agentConfig)

    @recordsize
    def getDatadogData(self):
        result = {}
        if self._datadogs is not None:
            for dd in self._datadogs:
                result[dd.key] = dd.check(self.checksLogger, self.agentConfig)

        return result
        
    @recordsize
    def getCassandraData(self):
        return self._cassandra.check(self.checksLogger, self.agentConfig)

    #
    # CPU Stats
    #
    @recordsize
    def getCPUStats(self):
        return self._cpu.check(self.checksLogger, self.agentConfig)
        
    #
    # Plugins
    #
        
    def getPlugins(self):
        self.checksLogger.debug('getPlugins: start')
        
        if 'pluginDirectory' in self.agentConfig:
            if os.path.exists(self.agentConfig['pluginDirectory']) == False:
                self.checksLogger.debug('getPlugins: ' + self.agentConfig['pluginDirectory'] + ' directory does not exist')
                return False
        else:
            return False
        
        # Have we already imported the plugins?
        # Only load the plugins once
        if self.plugins == None:
            self.checksLogger.debug('getPlugins: initial load from ' + self.agentConfig['pluginDirectory'])
            
            sys.path.append(self.agentConfig['pluginDirectory'])
            
            self.plugins = []
            plugins = []
            
            # Loop through all the plugin files
            for root, dirs, files in os.walk(self.agentConfig['pluginDirectory']):
                for name in files:
                    self.checksLogger.debug('getPlugins: considering: ' + name)
                
                    name = name.split('.', 1)
                    
                    # Only pull in .py files (ignores others, inc .pyc files)
                    try:
                        if name[1] == 'py':
                            
                            self.checksLogger.debug('getPlugins: ' + name[0] + '.' + name[1] + ' is a plugin')
                            
                            plugins.append(name[0])
                            
                    except IndexError, e:
                        
                        continue
            
            # Loop through all the found plugins, import them then create new objects
            for pluginName in plugins:
                self.checksLogger.debug('getPlugins: importing ' + pluginName)
                
                # Import the plugin, but only from the pluginDirectory (ensures no conflicts with other module names elsehwhere in the sys.path
                import imp
                importedPlugin = imp.load_source(pluginName, os.path.join(self.agentConfig['pluginDirectory'], '%s.py' % pluginName))
                
                self.checksLogger.debug('getPlugins: imported ' + pluginName)
                
                try:
                    # Find out the class name and then instantiate it
                    pluginClass = getattr(importedPlugin, pluginName)
                    
                    try:
                        pluginObj = pluginClass(self.agentConfig, self.checksLogger, self.rawConfig)
                    except TypeError:
                        
                        try:
                            pluginObj = pluginClass(self.agentConfig, self.checksLogger)
                        except TypeError:
                            # Support older plugins.
                            pluginObj = pluginClass()
                
                    self.checksLogger.debug('getPlugins: instantiated ' + pluginName)
                
                    # Store in class var so we can execute it again on the next cycle
                    self.plugins.append(pluginObj)
                except Exception, ex:
                    import traceback
                    self.checksLogger.error('getPlugins: exception = ' + traceback.format_exc())
                    
        # Now execute the objects previously created
        if self.plugins != None:            
            self.checksLogger.debug('getPlugins: executing plugins')
            
            # Execute the plugins
            output = {}
                    
            for plugin in self.plugins:             
                self.checksLogger.debug('getPlugins: executing ' + plugin.__class__.__name__)
                
                output[plugin.__class__.__name__] = plugin.run()
                
                self.checksLogger.debug('getPlugins: executed ' + plugin.__class__.__name__)
            
            self.checksLogger.debug('getPlugins: returning')
            
            # Each plugin should output a dictionary so we can convert it to JSON later 
            return output
            
        else:           
            self.checksLogger.debug('getPlugins: no plugins, returning false')
            
            return False
    
    #
    # Postback
    #
    
    def doChecks(self, sc, firstRun, systemStats=False):
        macV = None
        if sys.platform == 'darwin':
            macV = platform.mac_ver()
        
        if not self.os:
            if macV:
                self.os = 'mac'
            elif sys.platform.find('freebsd') != -1:
                self.os = 'freebsd'
            else:
                self.os = 'linux'
        
        self.checksLogger.debug('doChecks: start')
        
        # Do the checks
        apacheStatus = self.getApacheStatus()
        diskUsage = self.getDiskUsage()
        loadAvrgs = self.getLoadAvrgs()
        memory = self.getMemoryUsage()
        mysqlStatus = self.getMySQLStatus()
        networkTraffic = self.getNetworkTraffic()
        nginxStatus = self.getNginxStatus()
        processes = self.getProcesses()
        rabbitmq = self.getRabbitMQStatus()
        mongodb = self.getMongoDBStatus()
        couchdb = self.getCouchDBStatus()
        plugins = self.getPlugins()
        ioStats = self.getIOStats()
        cpuStats = self.getCPUStats()
        gangliaData = self.getGangliaData()
        datadogData = self.getDatadogData()
        cassandraData = self.getCassandraData()
 
        self.checksLogger.debug('doChecks: checks success, build payload')
        
        checksData = {
            'collection_timestamp': time.time(),
            'os' : self.os, 
            'agentKey' : self.agentConfig['agentKey'], 
            'agentVersion' : self.agentConfig['version'], 
            'diskUsage' : diskUsage, 
            'loadAvrg1' : loadAvrgs['1'], 
            'loadAvrg5' : loadAvrgs['5'], 
            'loadAvrg15' : loadAvrgs['15'], 
            'memPhysUsed' : memory['physUsed'], 
            'memPhysFree' : memory['physFree'], 
            'memSwapUsed' : memory['swapUsed'], 
            'memSwapFree' : memory['swapFree'], 
            'memCached' : memory['cached'], 
            'networkTraffic' : networkTraffic, 
            'processes' : processes,
            'apiKey': self.agentConfig['apiKey'],
            'events': {},
            'resources': {},
        }

        if cpuStats is not False and cpuStats is not None:
            checksData.update(cpuStats)

        if gangliaData is not False and gangliaData is not None:
            checksData['ganglia'] = gangliaData
           
        if datadogData is not False and datadogData is not None:
            checksData['datadog'] = datadogData
            
        if cassandraData is not False and cassandraData is not None:
            checksData['cassandra'] = cassandraData
 
        self.checksLogger.debug('doChecks: payload built, build optional payloads')
        
        # Apache Status
        if apacheStatus != False:           
            checksData['apacheReqPerSec'] = apacheStatus['reqPerSec']
            checksData['apacheBusyWorkers'] = apacheStatus['busyWorkers']
            checksData['apacheIdleWorkers'] = apacheStatus['idleWorkers']
            
            self.checksLogger.debug('doChecks: built optional payload apacheStatus')
        
        # MySQL Status
        if mysqlStatus != False:
            
            checksData['mysqlConnections'] = mysqlStatus['connections']
            checksData['mysqlCreatedTmpDiskTables'] = mysqlStatus['createdTmpDiskTables']
            checksData['mysqlMaxUsedConnections'] = mysqlStatus['maxUsedConnections']
            checksData['mysqlOpenFiles'] = mysqlStatus['openFiles']
            checksData['mysqlSlowQueries'] = mysqlStatus['slowQueries']
            checksData['mysqlTableLocksWaited'] = mysqlStatus['tableLocksWaited']
            checksData['mysqlThreadsConnected'] = mysqlStatus['threadsConnected']
            
            if mysqlStatus['secondsBehindMaster'] != None:
                checksData['mysqlSecondsBehindMaster'] = mysqlStatus['secondsBehindMaster']
        
        # Nginx Status
        if nginxStatus != False:
            checksData['nginxConnections'] = nginxStatus['connections']
            checksData['nginxReqPerSec'] = nginxStatus['reqPerSec']
            
        # RabbitMQ
        if rabbitmq != False:
            checksData['rabbitMQ'] = rabbitmq
        
        # MongoDB
        if mongodb != False:
            checksData['mongoDB'] = mongodb
            
        # CouchDB
        if couchdb != False:
            checksData['couchDB'] = couchdb
        
        # Plugins
        if plugins != False:
            checksData['plugins'] = plugins
        
        if ioStats != False:
            checksData['ioStats'] = ioStats
            
        # Include system stats on first postback
        if firstRun == True:
            checksData['systemStats'] = systemStats
            self.checksLogger.debug('doChecks: built optional payload systemStats')
            
        # Include server indentifiers
        import socket   
        
        try:
            checksData['internalHostname'] = socket.gethostname()
            
        except socket.error, e:
            self.checksLogger.debug('Unable to get hostname: ' + str(e))
        
        # Generate a unique name that will stay constant between
        # invocations, such as platform.node() + uuid.getnode()
        # Use uuid5, which does not depend on the clock and is
        # recommended over uuid3.
        # This is important to be able to identify a server even if
        # its drives have been wiped clean.
        # Note that this is not foolproof but we can reconcile servers
        # on the back-end if need be, based on mac addresses.
        checksData['uuid'] = uuid.uuid5(uuid.NAMESPACE_DNS, platform.node() + str(uuid.getnode())).hex
        self.checksLogger.debug('doChecks: added uuid %s' % checksData['uuid'])
        
        # Process the event checks. 
        for event_check in self._event_checks:
            event_data = event_check.check(self.checksLogger, self.agentConfig)
            if event_data:
                checksData['events'][event_check.key] = event_data

        # Resources checks
        has_resource = False
        for resources_check in self._resources_checks:
            resources_check.check()
            snap = resources_check.pop_snapshot()
            if snap:
                has_resource = True
                res_format = resources_check.describe_format_if_needed()
                res_value = { 'ts': snap[0],
                              'data': snap[1],
                              'format_version': resources_check.get_format_version() }                              
                if res_format is not None:
                    res_value['format_description'] = res_format
                checksData['resources'][resources_check.RESOURCE_KEY] = res_value
 
        if has_resource:
            checksData['resources']['meta'] = {
                        'api_key': self.agentConfig['apiKey'],
                        'host': checksData['internalHostname'],
                    }

        # Post a start event on firstrun 
        if firstRun:
            checksData['events']['System'] = [{'api_key': self.agentConfig['apiKey'],
                                              'host': checksData['internalHostname'],
                                              'timestamp': int(time.mktime(datetime.datetime.now().timetuple())),
                                              'event_type':'agent startup',
                                            }]
        
        self.emitter(checksData, self.checksLogger, self.agentConfig)
        
        sc.enter(self.agentConfig['checkFreq'], 1, self.doChecks, (sc, False))  
        
    def getMountedLinuxProcFsLocation(self):
        self.checksLogger.debug('getMountedLinuxProcFsLocation: attempting to fetch mounted partitions')
        
        # Lets check if the Linux like style procfs is mounted
        mountedPartitions = subprocess.Popen(['mount'], stdout = subprocess.PIPE, close_fds = True).communicate()[0]
        location = re.search(r'linprocfs on (.*?) \(.*?\)', mountedPartitions)
        
        # Linux like procfs file system is not mounted so we return False, else we return mount point location
        if location == None:
            return False

        location = location.group(1)
        return location
