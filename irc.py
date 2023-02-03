import logging
import socket
import random
import time
import ssl
import sys
import select
import datetime
import traceback

try:
    from PyQt6 import QtCore, QtGui
except ImportError:
    print("PyQt5 fallback (irc.py)")
    from PyQt5 import QtCore, QtGui

from mood import Mood
from dataobjs import PesterProfile
from generic import PesterList
from version import _pcVersion

from oyoyo import services
from oyoyo.ircevents import numeric_events

import scripts.irc.outgoing

PchumLog = logging.getLogger("pchumLogger")
SERVICES = [
    "nickserv",
    "chanserv",
    "memoserv",
    "operserv",
    "helpserv",
    "hostserv",
    "botserv",
]

class CommandError(Exception):
    def __init__(self, cmd):
        self.cmd = cmd


class NoSuchCommandError(CommandError):
    def __str__(self):
        return 'No such command "%s"' % ".".join(self.cmd)


class ProtectedCommandError(CommandError):
    def __str__(self):
        return 'Command "%s" is protected' % ".".join(self.cmd)

# Python 3
QString = str

try:
    import certifi
except ImportError:
    if sys.platform == "darwin":
        # Certifi is required to validate certificates on MacOS with pyinstaller builds.
        PchumLog.warning(
            "Failed to import certifi, which is recommended on MacOS. "
            "Pesterchum might not be able to validate certificates unless "
            "Python's root certs are installed."
        )
    else:
        PchumLog.info(
            "Failed to import certifi, Pesterchum will not be able to validate "
            "certificates if the system-provided root certificates are invalid."
        )

class IRCClientError(Exception):
    pass

class PesterIRC(QtCore.QThread):
    def __init__(self, config, window, verify_hostname=True):
        QtCore.QThread.__init__(self)
        self.mainwindow = window
        self.config = config
        self.unresponsive = False
        self.registeredIRC = False
        self.verify_hostname = verify_hostname
        self.metadata_supported = False
        self.stopIRC = None
        self.NickServ = services.NickServ()
        self.ChanServ = services.ChanServ()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.host = self.config.server()
        self.port = self.config.port()
        self.ssl = self.config.ssl()
        self.timeout = 120
        self.blocking = True
        self._end = False

        self.command_handler = self
        self.parent = self

        self.send_irc = scripts.irc.outgoing.SendIRC()

    def get_ssl_context(self):
        """Returns an SSL context for connecting over SSL/TLS.
        Loads the certifi root certificate bundle if the certifi module is less
        than a year old or if the system certificate store is empty.

        The cert store on Windows also seems to have issues, so it's better
        to use the certifi provided bundle assuming it's a recent version.

        On MacOS the system cert store is usually empty, as Python does not use
        the system provided ones, instead relying on a bundle installed with the
        python installer."""
        default_context = ssl.create_default_context()
        if "certifi" not in sys.modules:
            return default_context

        # Get age of certifi module
        certifi_date = datetime.datetime.strptime(certifi.__version__, "%Y.%m.%d")
        current_date = datetime.datetime.now()
        certifi_age = current_date - certifi_date

        empty_cert_store = (
            list(default_context.cert_store_stats().values()).count(0) == 3
        )
        # 31557600 seconds is approximately 1 year
        if empty_cert_store or certifi_age.total_seconds() <= 31557600:
            PchumLog.info(
                "Using SSL/TLS context with certifi-provided root certificates."
            )
            return ssl.create_default_context(cafile=certifi.where())
        PchumLog.info("Using SSL/TLS context with system-provided root certificates.")
        return default_context

    def connect(self, verify_hostname=True):
        """initiates the connection to the server set in self.host:self.port
        self.ssl decides whether the connection uses ssl.

        Certificate validation when using SSL/TLS may be disabled by
        passing the 'verify_hostname' parameter. The user is asked if they
        want to disable it if this functions raises a certificate validation error,
        in which case the function may be called again with 'verify_hostname'."""
        PchumLog.info("connecting to {}:{}".format(self.host, self.port))

        # Open connection
        plaintext_socket = socket.create_connection((self.host, self.port))

        if self.ssl:
            # Upgrade connection to use SSL/TLS if enabled
            context = self.get_ssl_context()
            context.check_hostname = verify_hostname
            self.socket = context.wrap_socket(
                plaintext_socket, server_hostname=self.host
            )
        else:
            # SSL/TLS is disabled, connection is plaintext
            self.socket = plaintext_socket

        self.send_irc.socket = self.socket

        # setblocking is a shorthand for timeout,
        # we shouldn't use both.
        if self.timeout:
            self.socket.settimeout(self.timeout)
        elif not self.blocking:
            self.socket.setblocking(False)
        elif self.blocking:
            self.socket.setblocking(True)

        self.send_irc.nick(self.mainwindow.profile().handle)
        self.send_irc.user("pcc31", "pcc31")
        # if self.connect_cb:
        #    self.connect_cb(self)

    def conn_generator(self):
        """returns a generator object."""
        try:
            buffer = b""
            while not self._end:
                data = []
                try:
                    buffer += self.socket.recv(1024)
                except OSError as e:
                    PchumLog.warning("conn exception {} in {}".format(e, self))
                    if self._end:
                        break
                    raise e
                else:
                    if self._end:
                        break
                    if not buffer and self.blocking:
                        PchumLog.debug("len(buffer) = 0")
                        raise OSError("Connection closed")
                    
                    data, buffer = self.parse_buffer(buffer)
                    print(f"data: {data}")
                    for line in data:
                        tags, prefix, command, args = self.parse_irc_line(line)
                        # print(tags, prefix, command, args)
                        try:
                            # Only need tags with tagmsg
                            if command.casefold() == "tagmsg":
                                self.run_command(command, prefix, tags, *args)
                            else:
                                self.run_command(command, prefix, *args)
                        except CommandError as e:
                            PchumLog.warning(f"CommandError: {e}")

                yield True
        except socket.timeout as se:
            PchumLog.debug("passing timeout")
            raise se
        except (OSError, ssl.SSLEOFError) as se:
            PchumLog.debug("problem: %s" % (str(se)))
            if self.socket:
                PchumLog.info("error: closing socket")
                self.socket.close()
            raise se
        except Exception as e:
            PchumLog.exception("Non-socket related exception in conn_generator().")
            raise e
        else:
            PchumLog.debug("ending while, end is %s" % self._end)
            if self.socket:
                PchumLog.info("finished: closing socket")
                self.socket.close()
            yield False

    def parse_buffer(self, buffer):
        """Parse lines from bytes buffer, returns lines and emptied buffer."""
        try:
            decoded_buffer = buffer.decode(encoding="utf-8")
        except UnicodeDecodeError as exception:
            PchumLog.warning(f"Failed to decode with utf-8, falling back to latin-1.")
            try:
                decoded_buffer = buffer.decode(encoding="latin-1")
            except ValueError as exception:
                PchumLog.warning("latin-1 failed too xd")
                return "", buffer  # throw it back in the cooker
            
        data = decoded_buffer.split("\r\n")
        if data[-1]:
            # Last entry has incomplete data, add back to buffer
            buffer = data[-1].encode(encoding="utf-8")
        return data[:-1], buffer

    def parse_irc_line(self, line: str):
        parts = line.split(" ")
        tags = None
        prefix = None
        if parts[0].startswith(":"):
            prefix = parts[0][1:]
            command = parts[1]
            args = parts[2:]
        elif parts[0].startswith("@"):
            tags = parts[0]  # IRCv3 message tag
            prefix = parts[1][1:]
            command = parts[2]
            args = parts[3:]
        else:
            command = parts[0]
            args = parts[1:]

        if command.isdigit():
            try:
                command = numeric_events[command]
            except KeyError:
                PchumLog.info("Server send unknown numeric event %s.", command)
        command = command.lower()

        # If ':' is present the subsequent args are one parameter.
        fused_args = []
        for idx, arg in enumerate(args):
            if arg.startswith(":"):
                final_param = ' '.join(args[idx:])
                fused_args.append(final_param[1:])
                break
            else:
                fused_args.append(arg)

        return (tags, prefix, command, fused_args)
    
    def close(self):
        # with extreme prejudice
        if self.socket:
            PchumLog.info("shutdown socket")
            # print("shutdown socket")
            self._end = True
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                PchumLog.debug(
                    "Error while shutting down socket, already broken? %s" % str(e)
                )
            try:
                self.socket.close()
            except OSError as e:
                PchumLog.debug(
                    "Error while closing socket, already broken? %s" % str(e)
                )

    def IRCConnect(self):
        try:
            self.connect(self.verify_hostname)
        except ssl.SSLCertVerificationError as e:
            # Ask if users wants to connect anyway
            self.askToConnect.emit(e)
            raise e
        self.conn = self.conn_generator()

    def run(self):
        try:
            self.IRCConnect()
        except OSError as se:
            self.stopIRC = se
            return
        while True:
            res = True
            try:
                PchumLog.debug("updateIRC()")
                self.mainwindow.sincerecv = 0
                res = self.updateIRC()
            except socket.timeout as se:
                PchumLog.debug("timeout in thread %s" % (self))
                self.close()
                self.stopIRC = "{}, {}".format(type(se), se)
                return
            except (OSError, IndexError, ValueError) as se:
                self.stopIRC = "{}, {}".format(type(se), se)
                PchumLog.debug("socket error, exiting thread")
                return
            else:
                if not res:
                    PchumLog.debug("false Yield: %s, returning" % res)
                    return

    def setConnected(self):
        self.registeredIRC = True
        self.connected.emit()

    def setConnectionBroken(self):
        PchumLog.critical("setconnection broken")
        self.disconnectIRC()
        # self.brokenConnection = True  # Unused

    @QtCore.pyqtSlot()
    def updateIRC(self):
        try:
            res = next(self.conn)
        except socket.timeout as se:
            if self.registeredIRC:
                return True
            else:
                raise se
        except OSError as se:
            raise se
        except (OSError, ValueError, IndexError) as se:
            raise se
        except StopIteration:
            self.conn = self.conn_generator()
            return True
        else:
            return res

    @QtCore.pyqtSlot(PesterProfile)
    def getMood(self, *chums):
        """Get mood via metadata if supported"""

        # Get via metadata or via legacy method
        if self.parent.metadata_supported:
            # Metadata
            for chum in chums:
                try:
                    self.send_irc.metadata(chum.handle, "get", "mood")
                except OSError as e:
                    PchumLog.warning(e)
                    self.parent.setConnectionBroken()
        else:
            # Legacy
            PchumLog.warning(
                "Server doesn't seem to support metadata, using legacy GETMOOD."
            )
            chumglub = "GETMOOD "
            for chum in chums:
                if len(chumglub + chum.handle) >= 350:
                    try:
                        self.send_irc.msg("#pesterchum", chumglub)
                    except OSError as e:
                        PchumLog.warning(e)
                        self.parent.setConnectionBroken()
                    chumglub = "GETMOOD "
                # No point in GETMOOD-ing services
                if chum.handle.casefold() not in SERVICES:
                    chumglub += chum.handle
            if chumglub != "GETMOOD ":
                try:
                    self.send_irc.msg("#pesterchum", chumglub)
                except OSError as e:
                    PchumLog.warning(e)
                    self.parent.setConnectionBroken()

    @QtCore.pyqtSlot(PesterList)
    def getMoods(self, chums):
        if hasattr(self, "cli"):
            self.command_handler.getMood(*chums)

    @QtCore.pyqtSlot(QString, QString)
    def sendNotice(self, text, handle):
        if hasattr(self, "cli"):
            h = str(handle)
            t = str(text)
            try:
                self.send_irc.notice(h, t)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString, QString)
    def sendMessage(self, text, handle):
        if hasattr(self, "cli"):
            h = str(handle)
            textl = [str(text)]

            def splittext(l):
                if len(l[0]) > 450:
                    space = l[0].rfind(" ", 0, 430)
                    if space == -1:
                        space = 450
                    elif l[0][space + 1 : space + 5] == "</c>":
                        space = space + 4
                    a = l[0][0 : space + 1]
                    b = l[0][space + 1 :]
                    if a.count("<c") > a.count("</c>"):
                        # oh god ctags will break!! D=
                        hanging = []
                        usedends = []
                        c = a.rfind("<c")
                        while c != -1:
                            d = a.find("</c>", c)
                            while d in usedends:
                                d = a.find("</c>", d + 1)
                            if d != -1:
                                usedends.append(d)
                            else:
                                f = a.find(">", c) + 1
                                hanging.append(a[c:f])
                            c = a.rfind("<c", 0, c)

                        # end all ctags in first part
                        for _ in range(a.count("<c") - a.count("</c>")):
                            a = a + "</c>"
                        # start them up again in the second part
                        for c in hanging:
                            b = c + b
                    if len(b) > 0:
                        return [a] + splittext([b])
                    else:
                        return [a]
                else:
                    return l

            textl = splittext(textl)
            try:
                for t in textl:
                    self.send_irc.msg(h, t)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(
        QString,
        QString,
    )
    def sendCTCP(self, handle, text):
        if hasattr(self, "cli"):
            try:
                self.send_irc.ctcp(handle, text)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString, bool)
    def startConvo(self, handle, initiated):
        if hasattr(self, "cli"):
            h = str(handle)
            try:
                self.send_irc.msg(h, "COLOR >%s" % (self.mainwindow.profile().colorcmd())
                )
                if initiated:
                    self.send_irc.msg(h, "PESTERCHUM:BEGIN")
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString)
    def endConvo(self, handle):
        if hasattr(self, "cli"):
            h = str(handle)
            try:
                self.send_irc.msg(h, "PESTERCHUM:CEASE")
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot()
    def updateProfile(self):
        if hasattr(self, "cli"):
            me = self.mainwindow.profile()
            handle = me.handle
            try:
                self.send_irc.nick(handle)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()
            self.mainwindow.closeConversations(True)
            self.mainwindow.doAutoIdentify()
            self.mainwindow.autoJoinDone = False
            self.mainwindow.doAutoJoins()
            self.updateMood()

    @QtCore.pyqtSlot()
    def updateMood(self):
        if hasattr(self, "cli"):
            me = self.mainwindow.profile()
            # Moods via metadata
            try:
                self.send_irc.metadata("*", "set", "mood", str(me.mood.value()))
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()
            # Backwards compatibility
            try:
                self.send_irc.msg("#pesterchum", "MOOD >%d" % (me.mood.value()))
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot()
    def updateColor(self):
        if hasattr(self, "cli"):
            # PchumLog.debug("irc updateColor (outgoing)")
            # me = self.mainwindow.profile()
            # Update color metadata field
            try:
                color = self.mainwindow.profile().color
                self.send_irc.metadata("*", "set", "color", str(color.name()))
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()
            # Send color messages
            for h in list(self.mainwindow.convos.keys()):
                try:
                    self.send_irc.msg(
                        h,
                        "COLOR >%s" % (self.mainwindow.profile().colorcmd()),
                    )
                except OSError as e:
                    PchumLog.warning(e)
                    self.setConnectionBroken()

    @QtCore.pyqtSlot(QString)
    def blockedChum(self, handle):
        if hasattr(self, "cli"):
            h = str(handle)
            try:
                self.send_irc.msg(h, "PESTERCHUM:BLOCK")
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString)
    def unblockedChum(self, handle):
        if hasattr(self, "cli"):
            h = str(handle)
            try:
                self.send_irc.msg(h, "PESTERCHUM:UNBLOCK")
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString)
    def requestNames(self, channel):
        if hasattr(self, "cli"):
            c = str(channel)
            try:
                self.send_irc.names(c)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot()
    def requestChannelList(self):
        if hasattr(self, "cli"):
            try:
                self.send_irc.channel_list(self)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString)
    def joinChannel(self, channel):
        if hasattr(self, "cli"):
            c = str(channel)
            try:
                self.send_irc.join(c)
                self.send_irc.mode(c, "", None)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString)
    def leftChannel(self, channel):
        if hasattr(self, "cli"):
            c = str(channel)
            try:
                self.send_irc.part(c)
                self.command_handler.joined = False
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString, QString)
    def kickUser(self, handle, channel):
        if hasattr(self, "cli"):
            l = handle.split(":")
            c = str(channel)
            h = str(l[0])
            if len(l) > 1:
                reason = str(l[1])
                if len(l) > 2:
                    for x in l[2:]:
                        reason += ":" + str(x)
            else:
                reason = ""
            try:
                self.send_irc.kick(channel, h, reason)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString, QString, QString)
    def setChannelMode(self, channel, mode, command):
        self.send_irc.mode(channel, mode, command)

    @QtCore.pyqtSlot(QString)
    def channelNames(self, channel):
        if hasattr(self, "cli"):
            c = str(channel)
            try:
                self.send_irc.names(c)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot(QString, QString)
    def inviteChum(self, handle, channel):
        if hasattr(self, "cli"):
            h = str(handle)
            c = str(channel)
            try:
                self.send_irc.invite(h, c)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot()
    def pingServer(self):
        try:
            self.send_irc.ping("B33")
        except OSError as e:
            PchumLog.warning(e)
            self.setConnectionBroken()

    @QtCore.pyqtSlot(bool)
    def setAway(self, away=True):
        try:
            if away:
                self.away("Idle")
            else:
                self.away()
        except OSError as e:
            PchumLog.warning(e)
            self.setConnectionBroken()

    @QtCore.pyqtSlot(QString, QString)
    def killSomeQuirks(self, channel, handle):
        if hasattr(self, "cli"):
            c = str(channel)
            h = str(handle)
            try:
                self.send_irc.ctcp(c, "NOQUIRKS", h)
            except OSError as e:
                PchumLog.warning(e)
                self.setConnectionBroken()

    @QtCore.pyqtSlot()
    def disconnectIRC(self):
        if hasattr(self, "cli"):
            self.send_irc.quit(_pcVersion + " <3")
            self._end = True
            self.close()

    
    def notice(self, nick, chan, msg):
        handle = nick[0 : nick.find("!")]
        PchumLog.info('---> recv "NOTICE {} :{}"'.format(handle, msg))
        if (
            handle == "ChanServ"
            and chan == self.parent.mainwindow.profile().handle
            and msg[0:2] == "[#"
        ):
            self.parent.memoReceived.emit(msg[1 : msg.index("]")], handle, msg)
        else:
            self.parent.noticeReceived.emit(handle, msg)

    def metadata(self, target, nick, key, visibility, value):
        # The format of the METADATA server notication is:
        # METADATA <Target> <Key> <Visibility> <Value>
        if key.lower() == "mood":
            try:
                mood = Mood(int(value))
                self.parent.moodUpdated.emit(nick, mood)
            except ValueError:
                PchumLog.warning("Invalid mood value, {}, {}".format(nick, mood))
        elif key.lower() == "color":
            color = QtGui.QColor(value)  # Invalid color becomes rgb 0,0,0
            self.parent.colorUpdated.emit(nick, color)

    def tagmsg(self, prefix, tags, *args):
        PchumLog.info("TAGMSG: {} {} {}".format(prefix, tags, str(args)))
        message_tags = tags[1:].split(";")
        for m in message_tags:
            if m.startswith("+pesterchum"):
                # Pesterchum tag
                try:
                    key, value = m.split("=")
                except ValueError:
                    return
                PchumLog.info("Pesterchum tag: {}={}".format(key, value))
                # PESTERCHUM: syntax check
                if (
                    (value == "BEGIN")
                    or (value == "BLOCK")
                    or (value == "CEASE")
                    or (value == "BLOCK")
                    or (value == "BLOCKED")
                    or (value == "UNBLOCK")
                    or (value == "IDLE")
                    or (value == "ME")
                ):
                    # Process like it's a PESTERCHUM: PRIVMSG
                    msg = "PESTERCHUM:" + value
                    self.privmsg(prefix, args[0], msg)
                elif value.startswith("COLOR>"):
                    # Process like it's a COLOR >0,0,0 PRIVMSG
                    msg = value.replace(">", " >")
                    self.privmsg(prefix, args[0], msg)
                elif value.startswith("TIME>"):
                    # Process like it's a PESTERCHUM:TIME> PRIVMSG
                    msg = "PESTERCHUM:" + value
                    self.privmsg(prefix, args[0], msg)
                else:
                    # Invalid syntax
                    PchumLog.warning("TAGMSG with invalid syntax.")

    def error(self, *params):
        # Server is ending connection.
        reason = ""
        for x in params:
            if (x != None) and (x != ""):
                reason += x + " "
        self.parent.stopIRC = reason.strip()
        self.parent.disconnectIRC()

    def privmsg(self, nick, chan, msg):
        handle = nick[0 : nick.find("!")]
        if len(msg) == 0:
            return

        # CTCP
        # ACTION, IRC /me (The CTCP kind)
        if msg[0:8] == "\x01ACTION ":
            msg = "/me" + msg[7:-1]
        # CTCPs that don't need to be shown
        elif msg[0] == "\x01":
            PchumLog.info('---> recv "CTCP {} :{}"'.format(handle, msg[1:-1]))
            # VERSION, return version
            if msg[1:-1].startswith("VERSION"):
                self.send_irc.ctcp(handle, "VERSION", "Pesterchum %s" % (_pcVersion)
                )
            # CLIENTINFO, return supported CTCP commands.
            elif msg[1:-1].startswith("CLIENTINFO"):
                self.send_irc.ctcp(
                    handle,
                    "CLIENTINFO",
                    "ACTION VERSION CLIENTINFO PING SOURCE NOQUIRKS GETMOOD",
                )
            # PING, return pong
            elif msg[1:-1].startswith("PING"):
                if len(msg[1:-1].split("PING ")) > 1:
                    self.send_irc.ctcp(handle, "PING", msg[1:-1].split("PING ")[1]
                    )
                else:
                    self.send_irc.ctcp(handle, "PING")
            # SOURCE, return source
            elif msg[1:-1].startswith("SOURCE"):
                self.send_irc.ctcp(
                    handle,
                    "SOURCE",
                    "https://github.com/Dpeta/pesterchum-alt-servers",
                )
            # ???
            elif msg[1:-1].startswith("NOQUIRKS") and chan[0] == "#":
                op = nick[0 : nick.find("!")]
                self.parent.quirkDisable.emit(chan, msg[10:-1], op)
            # GETMOOD via CTCP
            elif msg[1:-1].startswith("GETMOOD"):
                # GETMOOD via CTCP
                # Maybe we can do moods like this in the future...
                mymood = self.mainwindow.profile().mood.value()
                self.send_irc.ctcp(handle, "MOOD >%d" % (mymood))
                # Backwards compatibility
                self.send_irc.msg("#pesterchum", "MOOD >%d" % (mymood))
            return

        if chan != "#pesterchum":
            # We don't need anywhere near that much spam.
            PchumLog.info('---> recv "PRIVMSG {} :{}"'.format(handle, msg))

        if chan == "#pesterchum":
            # follow instructions
            if msg[0:6] == "MOOD >":
                try:
                    mood = Mood(int(msg[6:]))
                except ValueError:
                    mood = Mood(0)
                self.parent.moodUpdated.emit(handle, mood)
            elif msg[0:7] == "GETMOOD":
                mychumhandle = self.mainwindow.profile().handle
                mymood = self.mainwindow.profile().mood.value()
                if msg.find(mychumhandle, 8) != -1:
                    self.send_irc.msg("#pesterchum", "MOOD >%d" % (mymood))
        elif chan[0] == "#":
            if msg[0:16] == "PESTERCHUM:TIME>":
                self.parent.timeCommand.emit(chan, handle, msg[16:])
            else:
                self.parent.memoReceived.emit(chan, handle, msg)
        else:
            # private message
            # silently ignore messages to yourself.
            if handle == self.mainwindow.profile().handle:
                return
            if msg[0:7] == "COLOR >":
                colors = msg[7:].split(",")
                try:
                    colors = [int(d) for d in colors]
                except ValueError as e:
                    PchumLog.warning(e)
                    colors = [0, 0, 0]
                PchumLog.debug("colors: " + str(colors))
                color = QtGui.QColor(*colors)
                self.parent.colorUpdated.emit(handle, color)
            else:
                self.parent.messageReceived.emit(handle, msg)

    def pong(self, *args):
        # source, server, token
        # print("PONG", source, server, token)
        # self.parent.mainwindow.lastrecv = time.time()
        # print("PONG TIME: %s" % self.parent.mainwindow.lastpong)
        pass

    def welcome(self, server, nick, msg):
        self.parent.setConnected()
        # mychumhandle = self.mainwindow.profile().handle
        mymood = self.mainwindow.profile().mood.value()
        color = self.mainwindow.profile().color
        if not self.mainwindow.config.lowBandwidth():
            # Negotiate capabilities
            self.send_irc.cap("REQ", "message-tags")
            self.send_irc.cap(
                self, "REQ", "draft/metadata-notify-2"
            )  # <--- Not required in the unreal5 module implementation
            self.send_irc.cap(
                self, "REQ", "pesterchum-tag"
            )  # <--- Currently not using this
            time.sleep(0.413 + 0.097)  # <--- somehow, this actually helps.
            self.send_irc.join("#pesterchum")
            # Moods via metadata
            self.send_irc.metadata("*", "sub", "mood")
            self.send_irc.metadata("*", "set", "mood", str(mymood))
            # Color via metadata
            self.send_irc.metadata("*", "sub", "color")
            self.send_irc.metadata("*", "set", "color", str(color.name()))
            # Backwards compatible moods
            self.send_irc.msg("#pesterchum", "MOOD >%d" % (mymood))

    def erroneusnickname(self, *args):
        # Server is not allowing us to connect.
        reason = "Handle is not allowed on this server.\n"
        for x in args:
            if (x != None) and (x != ""):
                reason += x + " "
        self.parent.stopIRC = reason.strip()
        self.parent.disconnectIRC()

    def keyvalue(self, target, handle_us, handle_owner, key, visibility, *value):
        # The format of the METADATA server notication is:
        # METADATA <Target> <Key> <Visibility> <Value>
        if key == "mood":
            mood = Mood(int(value[0]))
            self.parent.moodUpdated.emit(handle_owner, mood)

    def metadatasubok(self, *params):
        PchumLog.info("metadatasubok: " + str(params))

    def nomatchingkey(self, target, our_handle, failed_handle, key, *error):
        # Try to get moods the old way if metadata fails.
        PchumLog.info("nomatchingkey: " + failed_handle)
        # No point in GETMOOD-ing services
        if failed_handle.casefold() not in SERVICES:
            try:
                self.send_irc.msg("#pesterchum", f"GETMOOD {failed_handle}")
            except OSError as e:
                PchumLog.warning(e)
                self.parent.setConnectionBroken()

    def keynotset(self, target, our_handle, failed_handle, key, *error):
        # Try to get moods the old way if metadata fails.
        PchumLog.info("nomatchingkey: " + failed_handle)
        chumglub = "GETMOOD "
        try:
            self.send_irc.msg("#pesterchum", chumglub + failed_handle)
        except OSError as e:
            PchumLog.warning(e)
            self.parent.setConnectionBroken()

    def keynopermission(self, target, our_handle, failed_handle, key, *error):
        # Try to get moods the old way if metadata fails.
        PchumLog.info("nomatchingkey: " + failed_handle)
        chumglub = "GETMOOD "
        try:
            self.send_irc.msg("#pesterchum", chumglub + failed_handle)
        except OSError as e:
            PchumLog.warning(e)
            self.parent.setConnectionBroken()

    def featurelist(self, target, handle, *params):
        # Better to do this via CAP ACK/CAP NEK
        # RPL_ISUPPORT
        features = params[:-1]
        PchumLog.info("Server featurelist: " + str(features))
        for x in features:
            if x.upper().startswith("METADATA"):
                PchumLog.info("Server supports metadata.")
                self.parent.metadata_supported = True

    def cap(self, server, nick, subcommand, tag):
        PchumLog.info("CAP {} {} {} {}".format(server, nick, subcommand, tag))
        # if tag == "message-tags":
        #    if subcommand == "ACK":

    def nicknameinuse(self, server, cmd, nick, msg):
        newnick = "pesterClient%d" % (random.randint(100, 999))
        self.send_irc.nick(newnick)
        self.parent.nickCollision.emit(nick, newnick)

    def nickcollision(self, server, cmd, nick, msg):
        newnick = "pesterClient%d" % (random.randint(100, 999))
        self.send_irc.nick(newnick)
        self.parent.nickCollision.emit(nick, newnick)

    def quit(self, nick, reason):
        handle = nick[0 : nick.find("!")]
        PchumLog.info('---> recv "QUIT {}: {}"'.format(handle, reason))
        if handle == self.parent.mainwindow.randhandler.randNick:
            self.parent.mainwindow.randhandler.setRunning(False)
        server = self.parent.mainwindow.config.server()
        baseserver = server[server.rfind(".", 0, server.rfind(".")) :]
        if reason.count(baseserver) == 2:
            self.parent.userPresentUpdate.emit(handle, "", "netsplit")
        else:
            self.parent.userPresentUpdate.emit(handle, "", "quit")
        self.parent.moodUpdated.emit(handle, Mood("offline"))

    def kick(self, opnick, channel, handle, reason):
        op = opnick[0 : opnick.find("!")]
        self.parent.userPresentUpdate.emit(
            handle, channel, "kick:{}:{}".format(op, reason)
        )
        # ok i shouldnt be overloading that but am lazy

    def part(self, nick, channel, reason="nanchos"):
        handle = nick[0 : nick.find("!")]
        PchumLog.info('---> recv "PART {}: {}"'.format(handle, channel))
        self.parent.userPresentUpdate.emit(handle, channel, "left")
        if channel == "#pesterchum":
            self.parent.moodUpdated.emit(handle, Mood("offline"))

    def join(self, nick, channel):
        handle = nick[0 : nick.find("!")]
        PchumLog.info('---> recv "JOIN {}: {}"'.format(handle, channel))
        self.parent.userPresentUpdate.emit(handle, channel, "join")
        if channel == "#pesterchum":
            if handle == self.parent.mainwindow.randhandler.randNick:
                self.parent.mainwindow.randhandler.setRunning(True)
            self.parent.moodUpdated.emit(handle, Mood("chummy"))

    def mode(self, op, channel, mode, *handles):
        PchumLog.debug("op=" + str(op))
        PchumLog.debug("channel=" + str(channel))
        PchumLog.debug("mode=" + str(mode))
        PchumLog.debug("*handles=" + str(handles))

        if len(handles) <= 0:
            handles = [""]
        opnick = op[0 : op.find("!")]
        PchumLog.debug("opnick=" + opnick)

        # Channel section
        # Okay so, as I understand it channel modes will always be applied to a channel even if the commands also sets a mode to a user.
        # So "MODE #channel +ro handleHandle" will set +r to channel #channel as well as set +o to handleHandle
        # Therefore the bellow method causes a crash if both user and channel mode are being set in one command.

        # if op == channel or channel == self.parent.mainwindow.profile().handle:
        #    modes = list(self.parent.mainwindow.modes)
        #    if modes and modes[0] == "+": modes = modes[1:]
        #    if mode[0] == "+":
        #        for m in mode[1:]:
        #            if m not in modes:
        #                modes.extend(m)
        #    elif mode[0] == "-":
        #        for i in mode[1:]:
        #            try:
        #                modes.remove(i)
        #            except ValueError:
        #                pass
        #    modes.sort()
        #    self.parent.mainwindow.modes = "+" + "".join(modes)

        # EXPIRIMENTAL FIX
        # No clue how stable this is but since it doesn't seem to cause a crash it's probably an improvement.
        # This might be clunky with non-unrealircd IRC servers
        channel_mode = ""
        unrealircd_channel_modes = [
            "c",
            "C",
            "d",
            "f",
            "G",
            "H",
            "i",
            "k",
            "K",
            "L",
            "l",
            "m",
            "M",
            "N",
            "n",
            "O",
            "P",
            "p",
            "Q",
            "R",
            "r",
            "s",
            "S",
            "T",
            "t",
            "V",
            "z",
            "Z",
        ]
        if any(md in mode for md in unrealircd_channel_modes):
            PchumLog.debug("Channel mode in string.")
            modes = list(self.parent.mainwindow.modes)
            for md in unrealircd_channel_modes:
                if mode.find(md) != -1:  # -1 means not found
                    PchumLog.debug("md=" + md)
                    if mode[0] == "+":
                        modes.extend(md)
                        channel_mode = "+" + md
                    elif mode[0] == "-":
                        try:
                            modes.remove(md)
                            channel_mode = "-" + md
                        except ValueError:
                            PchumLog.warning(
                                "Can't remove channel mode that isn't set."
                            )
                    self.parent.userPresentUpdate.emit(
                        "", channel, channel_mode + ":%s" % (op)
                    )
                    PchumLog.debug("pre-mode=" + str(mode))
                    mode = mode.replace(md, "")
                    PchumLog.debug("post-mode=" + str(mode))
            modes.sort()
            self.parent.mainwindow.modes = "+" + "".join(modes)

        modes = []
        cur = "+"
        for l in mode:
            if l in ["+", "-"]:
                cur = l
            else:
                modes.append("{}{}".format(cur, l))
        PchumLog.debug("handles=" + str(handles))
        PchumLog.debug("enumerate(modes) = " + str(list(enumerate(modes))))
        for (i, m) in enumerate(modes):

            # Server-set usermodes don't need to be passed.
            if (handles == [""]) & (
                ("x" in m) | ("z" in m) | ("o" in m) | ("x" in m)
            ) != True:
                try:
                    self.parent.userPresentUpdate.emit(
                        handles[i], channel, m + ":%s" % (op)
                    )
                except IndexError as e:
                    PchumLog.exception("modeSetIndexError: %s" % e)
            # print("i = " + i)
            # print("m = " + m)
            # self.parent.userPresentUpdate.emit(handles[i], channel, m+":%s" % (op))
            # self.parent.userPresentUpdate.emit(handles[i], channel, m+":%s" % (op))
            # Passing an empty handle here might cause a crash.
            # except IndexError:
            # self.parent.userPresentUpdate.emit("", channel, m+":%s" % (op))

    def nick(self, oldnick, newnick, hopcount=0):
        PchumLog.info("{}, {}".format(oldnick, newnick))
        # svsnick
        if oldnick == self.mainwindow.profile().handle:
            # Server changed our handle, svsnick?
            self.parent.getSvsnickedOn.emit(oldnick, newnick)

        # etc.
        oldhandle = oldnick[0 : oldnick.find("!")]
        if (oldhandle == self.mainwindow.profile().handle) or (
            newnick == self.mainwindow.profile().handle
        ):
            # print('hewwo')
            self.parent.myHandleChanged.emit(newnick)
        newchum = PesterProfile(newnick, chumdb=self.mainwindow.chumdb)
        self.parent.moodUpdated.emit(oldhandle, Mood("offline"))
        self.parent.userPresentUpdate.emit(
            "{}:{}".format(oldhandle, newnick), "", "nick"
        )
        if newnick in self.mainwindow.chumList.chums:
            self.getMood(newchum)
        if oldhandle == self.parent.mainwindow.randhandler.randNick:
            self.parent.mainwindow.randhandler.setRunning(False)
        elif newnick == self.parent.mainwindow.randhandler.randNick:
            self.parent.mainwindow.randhandler.setRunning(True)

    def namreply(self, server, nick, op, channel, names):
        namelist = names.split(" ")
        PchumLog.info('---> recv "NAMES %s: %d names"' % (channel, len(namelist)))
        if not hasattr(self, "channelnames"):
            self.channelnames = {}
        if channel not in self.channelnames:
            self.channelnames[channel] = []
        self.channelnames[channel].extend(namelist)

    # def ison(self, server, nick, nicks):
    #    nicklist = nicks.split(" ")
    #    getglub = "GETMOOD "
    #    PchumLog.info("---> recv \"ISON :%s\"" % nicks)
    #    for nick_it in nicklist:
    #        self.parent.moodUpdated.emit(nick_it, Mood(0))
    #        if nick_it in self.parent.mainwindow.namesdb["#pesterchum"]:
    #           getglub += nick_it
    #    if getglub != "GETMOOD ":
    #        self.send_irc.msg("#pesterchum", getglub)

    def endofnames(self, server, nick, channel, msg):
        try:
            namelist = self.channelnames[channel]
        except KeyError:
            # EON seems to return with wrong capitalization sometimes?
            for cn in self.channelnames.keys():
                if channel.lower() == cn.lower():
                    channel = cn
                    namelist = self.channelnames[channel]
        pl = PesterList(namelist)
        del self.channelnames[channel]
        self.parent.namesReceived.emit(channel, pl)
        if channel == "#pesterchum" and (
            not hasattr(self, "joined") or not self.joined
        ):
            self.joined = True
            self.parent.mainwindow.randhandler.setRunning(
                self.parent.mainwindow.randhandler.randNick in namelist
            )
            chums = self.mainwindow.chumList.chums
            # self.isOn(*chums)
            lesschums = []
            for c in chums:
                chandle = c.handle
                if chandle in namelist:
                    lesschums.append(c)
            self.getMood(*lesschums)

    def liststart(self, server, handle, *info):
        self.channel_list = []
        info = list(info)
        self.channel_field = info.index("Channel")  # dunno if this is protocol
        PchumLog.info('---> recv "CHANNELS: %s ' % (self.channel_field))

    def list(self, server, handle, *info):
        channel = info[self.channel_field]
        usercount = info[1]
        if channel not in self.channel_list and channel != "#pesterchum":
            self.channel_list.append((channel, usercount))
        PchumLog.info('---> recv "CHANNELS: %s ' % (channel))

    def listend(self, server, handle, msg):
        pl = PesterList(self.channel_list)
        PchumLog.info('---> recv "CHANNELS END"')
        self.parent.channelListReceived.emit(pl)
        self.channel_list = []

    def umodeis(self, server, handle, modes):
        self.parent.mainwindow.modes = modes

    def invite(self, sender, you, channel):
        handle = sender.split("!")[0]
        self.parent.inviteReceived.emit(handle, channel)

    def inviteonlychan(self, server, handle, channel, msg):
        self.parent.chanInviteOnly.emit(channel)

    # channelmodeis can have six arguments.
    def channelmodeis(self, server, handle, channel, modes, mode_params=""):
        self.parent.modesUpdated.emit(channel, modes)

    def cannotsendtochan(self, server, handle, channel, msg):
        self.parent.cannotSendToChan.emit(channel, msg)

    def toomanypeeps(self, *stuff):
        self.parent.tooManyPeeps.emit()

    # def badchanmask(channel, *args):
    #    # Channel name is not valid.
    #    msg = ' '.join(args)
    #    self.parent.forbiddenchannel.emit(channel, msg)
    def forbiddenchannel(self, server, handle, channel, msg):
        # Channel is forbidden.
        self.parent.forbiddenchannel.emit(channel, msg)
        self.parent.userPresentUpdate.emit(handle, channel, "left")

    def ping(self, prefix, token):
        """Respond to server PING with PONG."""
        self.send_irc.pong(token)

    def get(self, in_command_parts):
        PchumLog.debug("in_command_parts: %s" % in_command_parts)
        """ finds a command 
        commands may be dotted. each command part is checked that it does
        not start with and underscore and does not have an attribute 
        "protected". if either of these is true, ProtectedCommandError
        is raised.
        its possible to pass both "command.sub.func" and 
        ["command", "sub", "func"].
        """
        if isinstance(in_command_parts, (str, bytes)):
            in_command_parts = in_command_parts.split(".")
        command_parts = in_command_parts[:]

        p = self
        while command_parts:
            cmd = command_parts.pop(0)
            if cmd.startswith("_"):
                raise ProtectedCommandError(in_command_parts)

            try:
                f = getattr(p, cmd)
            except AttributeError:
                raise NoSuchCommandError(in_command_parts)

            if hasattr(f, "protected"):
                raise ProtectedCommandError(in_command_parts)

            #if isinstance(f, self) and command_parts:
            if command_parts:
                return f.get(command_parts)
            p = f

        return f

    def run_command(self, command, *args):
        """finds and runs a command"""
        PchumLog.debug("processCommand {}({})".format(command, args))

        try:
            f = self.get(command)
        except NoSuchCommandError as e:
            PchumLog.info(e)
            self.__unhandled__(command, *args)
            return

        PchumLog.debug("f %s" % f)

        try:
            f(*args)
        except TypeError as e:
            PchumLog.info(
                "Failed to pass command, did the server pass an unsupported paramater? "
                + str(e)
            )
        except Exception as e:
            # logging.info("Failed to pass command, %s" % str(e))
            PchumLog.exception("Failed to pass command")

    def __unhandled__(self, cmd, *args):
        """The default handler for commands. Override this method to
        apply custom behavior (example, printing) unhandled commands.
        """
        PchumLog.debug("unhandled command {}({})".format(cmd, args))

    moodUpdated = QtCore.pyqtSignal("QString", Mood)
    colorUpdated = QtCore.pyqtSignal("QString", QtGui.QColor)
    messageReceived = QtCore.pyqtSignal("QString", "QString")
    memoReceived = QtCore.pyqtSignal("QString", "QString", "QString")
    noticeReceived = QtCore.pyqtSignal("QString", "QString")
    inviteReceived = QtCore.pyqtSignal("QString", "QString")
    timeCommand = QtCore.pyqtSignal("QString", "QString", "QString")
    namesReceived = QtCore.pyqtSignal("QString", PesterList)
    channelListReceived = QtCore.pyqtSignal(PesterList)
    nickCollision = QtCore.pyqtSignal("QString", "QString")
    getSvsnickedOn = QtCore.pyqtSignal("QString", "QString")
    myHandleChanged = QtCore.pyqtSignal("QString")
    chanInviteOnly = QtCore.pyqtSignal("QString")
    modesUpdated = QtCore.pyqtSignal("QString", "QString")
    connected = QtCore.pyqtSignal()
    askToConnect = QtCore.pyqtSignal(Exception)
    userPresentUpdate = QtCore.pyqtSignal("QString", "QString", "QString")
    cannotSendToChan = QtCore.pyqtSignal("QString", "QString")
    tooManyPeeps = QtCore.pyqtSignal()
    quirkDisable = QtCore.pyqtSignal("QString", "QString", "QString")
    forbiddenchannel = QtCore.pyqtSignal("QString", "QString")
