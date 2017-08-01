"""
Smart Virtual Thermostat python plugin for Domoticz

Author: Logread,
        adapted from the Vera plugin by Antor, see:
            http://www.antor.fr/apps/smart-virtual-thermostat-eng-2/?lang=en
            https://github.com/AntorFr/SmartVT

Version:    0.0.1: alpha
            0.0.2: beta, with new connection object from Domoticz Python plugin framework
            0.0.3: bug fixes + added thermostat temp update even if in Off or Forced modes
            0.0.4: more logging parameters
            0.1.0: Use the standard urllib.request module to call the Domoticz API due to ongoing changes to
                    the Domoticz.Connection method
            0.1.1: Address strange behavior of python plugin framework where no more than a couple of devices can be
                    created and updated in one pass in the same function... so devices are created in "OnStart()" as
                    required but are only updated after all required devices are created
"""
"""
<plugin key="SVT" name="Smart Virtual Thermostat" author="logread" version="0.1.1" wikilink="https://www.domoticz.com/wiki/Plugins/Smart_Virtual_Thermostat.html" externallink="https://github.com/999LV/SmartVirtualThermostat.git">
    <params>
        <param field="Address" label="Domoticz IP Address" width="200px" required="true" default="127.0.0.1"/>
        <param field="Port" label="Port" width="40px" required="true" default="8080"/>
        <param field="Mode1" label="Inside Temperature Sensors (csv list of idx)" width="100px" required="true" default="0"/>
        <param field="Mode2" label="Outside Temperature Sensors (csv list of idx)" width="100px" required="false" default=""/>
        <param field="Mode3" label="Heating Switches (csv list of idx)" width="100px" required="true" default="0"/>
        <param field="Mode5" label="Calculation cycle, Minimum Heating time per cycle, Pause On delay, Pause Off delay, Forced mode duration (all in minutes)" width="200px" required="true" default="30,0,2,1,60"/>
        <param field="Mode6" label="Logging Level" width="75px">
            <options>
                <option label="Debug" value="Debug"/>
                <option label="Verbose" value="Verbose"/>
                <option label="Normal" value="Normal"  default="true" />
            </options>
        </param>
    </params>
</plugin>
"""
import Domoticz
import json
import urllib.parse as parse
import urllib.request as request
from datetime import datetime, timedelta

class deviceparam:

    def __init__(self, unit, nvalue, svalue):
        self.unit = unit
        self.nvalue = nvalue
        self.svalue = svalue


class BasePlugin:

    def __init__(self):
        self.debug = False
        self.calculate_period = 30  # Time in minutes between two calculations (cycle)
        self.minheattime = 0  # if heating is needed, minimum time of heating in % of cycle
        self.deltamax = 0.2  # allowed temp excess over setpoint temperature
        self.pauseondelay = 2  # time between pause sensor actuation and actual pause
        self.pauseoffdelay = 1  # time between end of pause sensor actuation and end of actual pause
        self.forcedduration = 60  # time in minutes for the forced mode
        self.update_period = 59  # time in minutes to refresh the setpoint devices so that these do not turn red
        self.InTempSensors = []
        self.OutTempSensors = []
        self.Heaters = []
        self.InternalsDefaults = {
            'ConstC': 60,  # inside heating coeff, depends on room size & power of your heater (60 by default)
            'ConstT': 1,  # external heating coeff,depends on the insulation relative to the outside (1 by default)
            'nbCC': 0,  # number of learnings for ConstC
            'nbCT': 0,  # number of learnings for ConstT
            'LastPwr': 0,  # % power from last calculation
            'LastInT': 0,  # inside temperature at last calculation
            'LastOutT': 0,  # outside temprature at last calculation
            'LastSetPoint': 20,  # setpoint at time of last calculation
            'ALStatus': 0}  # AutoLearning status (0 = uninitialized, 1 = initialized, 2 = disabled)
        self.Internals = self.InternalsDefaults.copy()
        self.heat = False
        self.pause = False
        self.pauserequested = False
        self.pauserequestchangedtime = datetime.now()
        self.forced = False
        self.intemp = 20.0
        self.outtemp = 20.0
        self.setpoint = 20.0
        self.endheat = datetime.now()
        self.nextcalc = self.endheat
        self.lastcalc = self.endheat
        self.nextupdate = self.endheat
        self.nexttemps = self.endheat
        return


    def onStart(self):
        if Parameters["Mode6"] == 'Debug':
            self.debug = True
            Domoticz.Debugging(1)
            DumpConfigToLog()
        else:
            Domoticz.Debugging(0)

        # create the child devices if these do not exist yet
        devicecreated = []
        if 1 not in Devices:
            Options = {"LevelActions": "||",
                       "LevelNames": "Off|Auto|Forced",
                       "LevelOffHidden": "false",
                       "SelectorStyle": "0"}
            Domoticz.Device(Name="Thermostat Control", Unit=1, TypeName="Selector Switch", Switchtype=18, Image=15,
                            Options=Options, Used=1).Create()
            #Devices[1].Update(nValue=0, sValue="0")  # default is Off state
            devicecreated.append(deviceparam(1, 0, "0"))  # default is Off state
        if 2 not in Devices:
            Options = {"LevelActions": "||",
                       "LevelNames": "Off|Normal|Economy",
                       "LevelOffHidden": "true",
                       "SelectorStyle": "0"}
            Domoticz.Device(Name="Thermostat Mode", Unit=2, TypeName="Selector Switch", Switchtype=18, Image=15,
                            Options=Options, Used=1).Create()
            #Devices[2].Update(nValue=0, sValue="10")  # default is normal mode
            devicecreated.append(deviceparam(2, 0, "10"))  # default is normal mode
        if 3 not in Devices:
            Domoticz.Device(Name="Thermostat Pause", Unit=3, TypeName="Switch", Image=9, Used=1).Create()
            #Devices[3].Update(nValue=0, sValue="")  # default is Off
            devicecreated.append(deviceparam(3, 0, ""))  # default is Off
        if 4 not in Devices:
            Domoticz.Device(Name="Setpoint Normal", Unit=4, Type=242, Subtype=1, Used=1).Create()
            #Devices[4].Update(nValue=0, sValue="20")  # default is 20 degrees
            devicecreated.append(deviceparam(4, 0, "20"))  # default is 20 degrees
        if 5 not in Devices:
            Domoticz.Device(Name="Setpoint Economy", Unit=5, Type=242, Subtype=1, Used=1).Create()
            #Devices[5].Update(nValue=0, sValue="20")  # default is 20 degrees
            devicecreated.append(deviceparam(5 ,0, "20"))  # default is 20 degrees
        if 6 not in Devices:
            Domoticz.Device(Name="Thermostat temp", Unit=6, TypeName="Temperature").Create()
            #Devices[6].Update(nValue=0, sValue="20")  # default is 20 degrees
            devicecreated.append(deviceparam(6, 0, "20"))  # default is 20 degrees

        # if any device has been created in onStart(), now is time to update its defaults
        # since doing so creates an error if more than two devices are created and updated at once
        for device in devicecreated:
            Devices[device.unit].Update(nValue=device.nvalue, sValue=device.svalue)

        # build lists of sensors and switches
        self.InTempSensors = parseCSV(Parameters["Mode1"])
        Domoticz.Debug("Inside Temperature sensors = {}".format(self.InTempSensors))
        self.OutTempSensors = parseCSV(Parameters["Mode2"])
        Domoticz.Debug("Outside Temperature sensors = {}".format(self.OutTempSensors))
        self.Heaters = parseCSV(Parameters["Mode3"])
        Domoticz.Debug("Heaters = {}".format(self.Heaters))

        # splits additional parameters
        params = parseCSV(Parameters["Mode5"])
        if len(params) == 5:
            self.calculate_period = params[0]
            self.minheattime = params[1]
            self.pauseondelay = params[2]
            self.pauseoffdelay = params[3]
            self.forcedduration = params[4]
        else:
            Domoticz.Error("Error reading Mode5 parameters")

        # loads persistent variables from dedicated user variable
        # note: to reset the thermostat to default values (i.e. ignore all past learning),
        # just delete the relevant "<plugin name>-InternalVariables" user variable Domoticz GUI and restart plugin
        self.getUserVar()


    def onStop(self):
        Domoticz.Debugging(0)


    def onCommand(self, Unit, Command, Level, Hue):
        Domoticz.Debug("onCommand called for Unit {}: Command '{}', Level: {}".format(Unit, Command, Level))
        if Unit == 3:
            self.pauserequestchangedtime = datetime.now()
            svalue = ""
            if str(Command) == "On":
                nvalue = 1
                self.pauserequested = True
            else:
                nvalue = 0
                self.pauserequested = False
        else:
            if Level > 0:
                nvalue = 1
            else:
                nvalue = 0
            svalue = str(Level)
        Devices[Unit].Update(nValue=nvalue, sValue=svalue)
        if Unit == 1:
            self.nextcalc = datetime.now()
            self.onHeartbeat()


    def onHeartbeat(self):

        now = datetime.now()
        # counter to update the temps in modes Off and Forced (in Auto mode, this is not needed nor used)
        if self.nexttemps <= now:
            self.nexttemps = now + timedelta(minutes=self.calculate_period)
            Domoticz.Debug("Thermostat temperature update called")
            updatetemps = True
        else:
            updatetemps = False

        if Devices[1].sValue == "0":  # Thermostat is off
            if self.forced:  # thermostat setting was just changed from "forced" so we kill the forced mode
                self.forced = False
                self.endheat = now
                Domoticz.Debug("Forced mode Off !")
                self.switchHeat(False)
            elif self.heat:
                self.endheat = now
                Domoticz.Debug("Switching heat Off !")
                self.switchHeat(False)
            else:
                Domoticz.Debug("Thermostat is off")
            if updatetemps:
                # call the Domoticz json API for a temperature devices update, to get the lastest temps...
                self.readTemps(DomoticzAPI("/json.htm?type=devices&filter=temp&used=true&order=Name"))


        elif Devices[1].sValue == "20":  # Thermostat is in forced mode
            if self.forced:
                if self.endheat <= now:
                    self.forced = False
                    self.endheat = now
                    Domoticz.Debug("Forced mode Off !")
                    Devices[1].Update(nValue=1, sValue="10")  # set thermostat to normal mode
                    self.switchHeat(False)
            else:
                self.forced = True
                self.endheat = now + timedelta(minutes=self.forcedduration)
                Domoticz.Debug("Forced mode On !")
                self.switchHeat(True)
            if updatetemps:
                # call the Domoticz json API for a temperature devices update, to get the lastest temps...
                self.readTemps(DomoticzAPI("/json.htm?type=devices&filter=temp&used=true&order=Name"))

        else:  # Thermostat is in mode auto

            if self.forced:  # thermostat setting was just changed from "forced" so we kill the forced mode
                self.forced = False
                self.endheat = now
                self.nextcalc = now + timedelta(minutes=self.calculate_period)
                Domoticz.Debug("Forced mode Off !")
                self.switchHeat(False)

            elif (self.endheat <= now or self.pause) and self.heat:  # heat cycle is over
                self.endheat = now
                self.switchHeat(False)

            elif self.pause and not self.pauserequested:  # we are in pause and the pause switch is now off
                if self.pauserequestchangedtime + timedelta(minutes=self.pauseoffdelay) <= now:
                    Domoticz.Debug("Pause is now Off")
                    self.pause = False

            elif not self.pause and self.pauserequested:  # we are not in pause and the pause switch is now on
                if self.pauserequestchangedtime + timedelta(minutes=self.pauseondelay) <= now:
                    Domoticz.Debug("Pause is now On")
                    self.pause = True
                    self.switchHeat(False)

            elif (self.nextcalc <= now) and not self.pause:  # we start a new calculation
                self.nextcalc = now + timedelta(minutes=self.calculate_period)
                Domoticz.Debug("Next calculation time will be : " + str(self.nextcalc))
                if Devices[2].sValue == "10":  # make setpoint reflect the select mode (10= normal, 20 = economy)
                    self.setpoint = float(Devices[4].sValue)
                else:
                    self.setpoint = float(Devices[5].sValue)
                # call the Domoticz json API for a temperature devices update, to get the lastest temps...
                self.readTemps(DomoticzAPI("/json.htm?type=devices&filter=temp&used=true&order=Name"))
                # do the thermostat work
                self.AutoMode()

        # check if need to refresh setpoints so that they do not turn red in GUI
        if self.nextupdate <= now:
            self.nextupdate = now + timedelta(minutes=self.update_period)
            Devices[4].Update(nValue=0, sValue=Devices[4].sValue)
            Devices[5].Update(nValue=0, sValue=Devices[5].sValue)


    def AutoMode(self):
        if self.intemp > self.setpoint + self.deltamax:
            Domoticz.Debug("Temperature exceeds setpoint: no heating")
            self.switchHeat(False)
        else:
            self.AutoCallib()
            if self.outtemp is None:
                power = round((self.setpoint - self.intemp) * self.Internals["ConstC"], 1)
            else:
                power = round((self.setpoint - self.intemp) * self.Internals["ConstC"] +
                              (self.setpoint - self.outtemp) * self.Internals["ConstT"], 1)
            if power < 0:
                power = 0  # Limite basse
            if power > 100:
                power = 100  # Limite haute
            if (power > 0) and (power <= self.minheattime):
                power = 0  # Seuil mini de power
            heatduration = round(power * self.calculate_period / 100)
            Domoticz.Debug("Calculation: Power = {} -> heat duration = {}".format(power, heatduration))
            if power == 0:
                self.switchHeat(False)
                Domoticz.Debug("No heating required !")
            else:
                self.endheat = datetime.now() + timedelta(minutes=heatduration)
                Domoticz.Debug("End Heat time = " + str(self.endheat))
                self.switchHeat(True)
                if self.Internals["ALStatus"] < 2:
                    self.Internals['LastPwr'] = power
                    self.Internals['LastInT'] = self.intemp
                    self.Internals['LastOutT'] = self.outtemp
                    self.Internals['LastSetPoint'] = self.setpoint
                    self.Internals['ALStatus'] = 1
                    self.saveUserVar()  # update user variables with latest learning
        self.lastcalc = datetime.now()


    def AutoCallib(self):
        now = datetime.now()
        if self.Internals['ALStatus'] != 1:  # not initalized... do nothing
            Domoticz.Debug("Fist pass at AutoCallib... no callibration")
            pass
        elif self.Internals['LastPwr'] == 0:  # heater was off last time, do nothing
            Domoticz.Debug("Last power was zero... no callibration")
            pass
        elif self.Internals['LastPwr'] == 100 and self.intemp < self.Internals['LastSetPoint']:
            # heater was on max but setpoint was not reached... no learning
            Domoticz.Debug("Last power was 100% but setpoint not reached... no callibration")
            pass
        elif self.intemp > self.Internals['LastInT'] and self.Internals['LastSetPoint'] > self.Internals['LastInT']:
            # learning ConstC
            ConstC = (self.Internals['ConstC'] * ((self.Internals['LastSetPoint'] - self.Internals['LastInT']) /
                                                  (self.intemp - self.Internals['LastInT']) *
                                                  ((now - self.lastcalc) / timedelta(minutes=self.calculate_period))))
            WriteLog("New calc for ConstC = {}".format(ConstC), "Verbose")
            self.Internals['ConstC'] = round((self.Internals['ConstC'] * self.Internals['nbCC'] + ConstC) /
                                             (self.Internals['nbCC'] + 1), 1)
            self.Internals['nbCC'] = min(self.Internals['nbCC'] + 1, 50)
            WriteLog("ConstC updated to {}".format(self.Internals['ConstC']), "Verbose")
        elif self.Internals['LastSetPoint'] > self.Internals['LastOutT']:
            # learning ConstT
            ConstT = (self.Internals['ConstT'] + ((self.Internals['LastSetPoint'] - self.intemp) /
                                                  (self.Internals['LastSetPoint'] - self.Internals['LastOutT']) *
                                                  self.Internals['ConstC'] *
                                                  ((now - self.lastcalc) / timedelta(minutes=self.calculate_period))))
            WriteLog("New calc for ConstT = {}".format(ConstT), "Verbose")
            self.Internals['ConstT'] = round((self.Internals['ConstT'] * self.Internals['nbCT'] + ConstT) /
                                             (self.Internals['nbCT'] + 1), 1)
            self.Internals['nbCT'] = min(self.Internals['nbCT'] + 1, 50)
            WriteLog("ConstT updated to {}".format(self.Internals['ConstT']), "Verbose")


    def switchHeat(self, switch):
        if switch:  # heating on
            self.heat = True
            Domoticz.Debug("Heating On")
            # switch on heater(s)
            for heater in self.Heaters:
                DomoticzAPI("/json.htm?type=command&param=switchlight&idx={}&switchcmd=On".format(heater))
            Domoticz.Debug("End Heat time = " + str(self.endheat))
        else:
            self.heat = False
            Domoticz.Debug("Heating Off")
            # switch off heater(s)
            for heater in self.Heaters:
                DomoticzAPI("/json.htm?type=command&param=switchlight&idx={}&switchcmd=Off".format(heater))


    def readTemps(self, devicesAPI):
        # fetch all the devices from the API and scan for sensors
        listintemps = []
        listouttemps = []
        for device in devicesAPI["result"]:  # parse the devices for temperature sensors
            idx = int(device["idx"])
            if idx in self.InTempSensors:
                if "Temp" in device:
                    Domoticz.Debug("device: {}-{} = {}".format(device["idx"], device["Name"], device["Temp"]))
                    listintemps.append(device["Temp"])
                else:
                    Domoticz.Error("device: {}-{} is not a Temperature sensor".format(device["idx"], device["Name"]))
            elif idx in self.OutTempSensors:
                if "Temp" in device:
                    WriteLog("device: {}-{} = {}".format(device["idx"], device["Name"], device["Temp"]), "Verbose")
                    listouttemps.append(device["Temp"])
                else:
                    Domoticz.Error("device: {}-{} is not a Temperature sensor".format(device["idx"], device["Name"]))

        # calculate the average inside temperature
        nbtemps = len(listintemps)
        if nbtemps > 0:
            self.intemp = round(sum(listintemps) / nbtemps, 1)
        else:
            Domoticz.Error("No Inside Temperature found... Switching Thermostat Off")
            Devices[1].Update(nValue=0, sValue="")  # switch off the thermostat

        # calculate the average outside temperature
        nbtemps = len(listouttemps)
        if nbtemps > 0:
            self.outtemp = round(sum(listouttemps) / nbtemps, 1)
        else:
            Domoticz.Debug("No Outside Temperature found...")
            self.outtemp = None

        WriteLog("Inside Temperature = {}".format(self.intemp), "Verbose")
        WriteLog("Outside Temperature = {}".format(self.outtemp), "Verbose")
        Devices[6].Update(nValue=0,
                          sValue=str(self.intemp))  # update the dummy device showing the current themostat temp


    def getUserVar(self):
        variables = DomoticzAPI("/json.htm?type=command&param=getuservariables")
        if variables:
            # there is a valid response from the API but we do not know if our variable exists yet
            novar = True
            varname = Parameters["Name"] + "-InternalVariables"
            valuestring = ""
            if "result" in variables:
                for variable in variables["result"]:
                    if variable["Name"] == varname:
                        valuestring = variable["Value"]
                        novar = False
                        break
            if novar:
                # create user variable since it does not exist
                WriteLog("User Variable {} does not exist. Creation requested".format(varname), "Verbose")
                DomoticzAPI("/json.htm?type=command&param=saveuservariable&vname={}&vtype=2&vvalue={}".format(
                    varname, str(self.InternalsDefaults)))
                self.Internals = self.InternalsDefaults.copy()  # we re-initialize the internal variables
            else:
                try:
                    self.Internals.update(eval(valuestring))
                except:
                    self.Internals = self.InternalsDefaults.copy()
                return
        else:
            Domoticz.Error("Cannot read the uservariable holding the persistent variables")
            self.Internals = self.InternalsDefaults.copy()


    def saveUserVar(self):
        varname = Parameters["Name"] + "-InternalVariables"
        DomoticzAPI("/json.htm?type=command&param=updateuservariable&vname={}&vtype=2&vvalue={}".format(
            varname, str(self.Internals)))


global _plugin
_plugin = BasePlugin()


def onStart():
    global _plugin
    _plugin.onStart()


def onStop():
    global _plugin
    _plugin.onStop()


def onCommand(Unit, Command, Level, Hue):
    global _plugin
    _plugin.onCommand(Unit, Command, Level, Hue)


def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()


# Plugin specific functions ---------------------------------------------------

def parseCSV(strCSV):
    listvals = []
    for value in strCSV.split(","):
        try:
            val = int(value)
        except:
            pass
        else:
            listvals.append(val)
    return listvals


def WriteLog(message, level="Normal"):
    if Parameters["Mode6"] == "Verbose":
        Domoticz.Log(message)
    elif level == "Normal":
        Domoticz.Log(message)


def DomoticzAPI(APICall):
    resultJson = None
    url = "http://{}:{}{}".format(Parameters["Address"], Parameters["Port"], parse.quote(APICall, safe="/&=?"))
    try:
        response = request.urlopen(url)
        if response.status == 200:
            resultJson = json.loads(response.read().decode('utf-8'))
            if resultJson["status"] != "OK":
                Domoticz.Error("Domoticz API returned an error: status = {}".format(resultJson["status"]))
                resultJson = None
        else:
            Domoticz.Error("Domoticz API: http error = {}".format(response.status))
    except:
        Domoticz.Error("Error calling '{}'".format(url))
        return ""
    return resultJson


# Generic helper functions
def DumpConfigToLog():
    for x in Parameters:
        if Parameters[x] != "":
            Domoticz.Debug("'" + x + "':'" + str(Parameters[x]) + "'")
    Domoticz.Debug("Device count: " + str(len(Devices)))
    for x in Devices:
        Domoticz.Debug("Device:           " + str(x) + " - " + str(Devices[x]))
        Domoticz.Debug("Device ID:       '" + str(Devices[x].ID) + "'")
        Domoticz.Debug("Device Name:     '" + Devices[x].Name + "'")
        Domoticz.Debug("Device nValue:    " + str(Devices[x].nValue))
        Domoticz.Debug("Device sValue:   '" + Devices[x].sValue + "'")
        Domoticz.Debug("Device LastLevel: " + str(Devices[x].LastLevel))
    return
