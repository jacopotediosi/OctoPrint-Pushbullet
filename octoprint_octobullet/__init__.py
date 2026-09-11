# coding=utf-8
from __future__ import absolute_import

__author__ = "Gina Häußge <gina@octoprint.org>, Adopted by Scott Rini <scott@rini.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2015 The OctoPrint Project - Released under terms of the AGPLv3 License"
__plugin_pythoncompat__ = ">=2.7,<4"

import os
import random
import string
import tempfile
import time
import octoprint.plugin

from octoprint.events import Events
from octoprint.access.permissions import Permissions

try:
	from octoprint.webcams import get_snapshot_webcam
except ImportError: # OctoPrint < 1.9.0
	get_snapshot_webcam = None

import pushbullet
import flask
import requests
import threading

from PIL import Image, ImageOps


_TIME_REMAINING_FORMAT = "{hours:d}h {minutes:d}min"
_TIME_DAYS_REMAINING_FORMAT = "{days:d}d {hours:d}h {minutes:d}min"
_ETA_STRFTIME = "%H:%M"
_ETA_DAYS_STRFTIME = "%Y-%m-%d %H:%M"
_PERIODIC_FILENAME_FORMAT = "{name}-{progress}.jpg"

_SECONDS_PER_DAY = 86400
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60


def _get_time_from_seconds(seconds, default=None):
	"""
	Tests:

		>>> _get_time_from_seconds(0)
		'0h 0min'
		>>> _get_time_from_seconds(86400 + 3600 + 60)
		'1d 1h 1min'
		>>> _get_time_from_seconds(23 * 3600 + 59 * 60 + 59)
		'23h 59min'
	"""

	if seconds is None:
		return default

	try:
		seconds = int(seconds)
	except ValueError:
		return default

	days, seconds = divmod(seconds, _SECONDS_PER_DAY)
	hours, seconds = divmod(seconds, _SECONDS_PER_HOUR)
	minutes, seconds = divmod(seconds, _SECONDS_PER_MINUTE)

	if days > 0:
		return _TIME_DAYS_REMAINING_FORMAT.format(**locals())
	else:
		return _TIME_REMAINING_FORMAT.format(**locals())


def _get_eta_from_seconds(seconds, default=None):
	if seconds is None:
		return default

	try:
		seconds = int(seconds)
	except ValueError:
		return default

	target_time = time.localtime(time.time() + seconds)
	if seconds > _SECONDS_PER_DAY:
		return time.strftime(_ETA_DAYS_STRFTIME, target_time)
	else:
		return time.strftime(_ETA_STRFTIME, target_time)


class PushbulletPlugin(octoprint.plugin.EventHandlerPlugin,
                       octoprint.plugin.ProgressPlugin,
                       octoprint.plugin.SettingsPlugin,
                       octoprint.plugin.StartupPlugin,
                       octoprint.plugin.TemplatePlugin,
                       octoprint.plugin.SimpleApiPlugin,
                       octoprint.plugin.AssetPlugin):

	def __init__(self):
		self._bullet = None
		self._channel = None
		self._sender = None

		self._periodic_updates = False
		self._periodic_updates_interval = 0
		self._next_message = None

		self._periodic_updates_lock = threading.RLock()

	def _connect_bullet(self, apikey, channel_name=""):
		try:
			self._bullet, self._sender = self._create_sender(apikey, channel=channel_name)
		except NoSuchChannel:
			self._logger.warning("Could not find channel {}, please check your configuration!".format(channel_name))
			self._bullet, self._sender = self._create_sender(apikey)
		except pushbullet.InvalidKeyError:
			self._logger.error("Invalid Pushbullet API key, please check your configuration!")
			self._bullet = self._sender = None

	#~~ progress message helpers

	@staticmethod
	def _get_progress_data(current_data):
		time_value = current_data["progress"]["printTime"]
		time_left_value = current_data["progress"]["printTimeLeft"]
		return time_value, time_left_value

	#~~ PrintProgressPlugin

	def on_print_progress(self, storage, path, progress):
		self._send_periodic_update(progress)

	#~~ StartupPlugin

	def on_after_startup(self):
		self._connect_bullet(self._settings.get(["access_token"]),
		                     self._settings.get(["push_channel"]))
		self._periodic_updates = self._settings.get(["periodic_updates"])
		self._periodic_updates_interval = self._settings.get_int(["periodic_updates_interval"]) * 60

	#~~ SettingsPlugin

	def on_settings_save(self, data):
		if "periodic_updates_interval" in data:
			try:
				data["periodic_updates_interval"] = int(data["periodic_updates_interval"])
			except:
				self._logger.exception("Got an invalid value to save for periodic_updates_interval, ignoring it")
				del data["periodic_updates_interval"]

		if "access_token" in data and not data["access_token"]:
			data["access_token"] = None

		if "push_channel" in data and not data["push_channel"]:
			data["push_channel"] = None

		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

		thread = threading.Thread(target=self._connect_bullet, args=(self._settings.get(["access_token"]),
		                                                             self._settings.get(["push_channel"])))
		thread.daemon = True
		thread.start()

		with self._periodic_updates_lock:
			# Periodic update settings
			self._periodic_updates = self._settings.get(["periodic_updates"])
			self._periodic_updates_interval = self._settings.get_int(["periodic_updates_interval"]) * 60

			# Changing settings mid-print resets the timer
			if self._next_message is not None:
				self._next_message = time.time() + self._periodic_updates_interval


	def get_settings_defaults(self):
		return dict(
			access_token=None,
			push_channel=None,
			periodic_updates = False,
			periodic_updates_interval = 15,
			printDone=dict(
				title="Print job finished",
				body="{file} finished printing in {elapsed_time}"
			),
			printProgress=dict(
				title="Print job {progress}% complete",
				body="{progress}% on {file}\nTime elapsed: {elapsed_time}\nTime left: {remaining_time}\nETA: {eta}"
			)
		)

	def get_settings_restricted_paths(self):
		return dict(admin=[["access_token"], ["push_channel"]])

	#~~ TemplatePlugin API

	def get_template_configs(self):
		return [
			dict(type="settings", name="Pushbullet", custom_bindings=True)
		]

	def is_template_autoescaped(self):
		return True

	#~~ AssetPlugin API

	def get_assets(self):
		return dict(js=["js/octobullet.js"])

	#~~ SimpleApiPlugin

	def get_api_commands(self):
		return dict(test=["token"])

	def is_api_protected(self):
		return True

	def on_api_command(self, command, data):
		if not Permissions.SETTINGS.can():
			return flask.make_response("Insufficient rights", 403)

		if not command == "test":
			return

		message = data.get("message", "Testing, 1, 2, 3, 4...")
		token = data["token"]
		channel = data.get("channel", None)

		try:
			_, sender = self._create_sender(token, channel=channel)
		except NoSuchChannel:
			return flask.make_response(flask.jsonify(result=False, error="channel"))
		except pushbullet.InvalidKeyError:
			return flask.make_response(flask.jsonify(result=False, error="apikey"))

		result = self._send_message_with_webcam_image("Test from the OctoPrint PushBullet Plugin", message, sender=sender)
		return flask.make_response(flask.jsonify(result=result))

	#~~ EventHandlerPlugin

	def on_event(self, event, payload):

		if event == Events.PRINT_DONE:
			path = os.path.basename(payload["name"])
			elapsed_time_in_seconds = payload["time"]

			placeholders = dict(file=path,
			                    elapsed_time=_get_time_from_seconds(elapsed_time_in_seconds, default="?"))

			title = self._settings.get(["printDone", "title"]).format(**placeholders)
			body = self._settings.get(["printDone", "body"]).format(**placeholders)
			filename = os.path.splitext(path)[0] + "-done.jpg"

			self._send_message_with_webcam_image(title, body, filename=filename)

			with self._periodic_updates_lock:
				self._next_message = None

		elif event == Events.PRINT_STARTED:
			if self._periodic_updates:
				with self._periodic_updates_lock:
					self._next_message = time.time() + self._periodic_updates_interval


	##~~ Softwareupdate hook

	def get_update_information(self):
		return dict(
			octobullet=dict(
				displayName="Pushbullet Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="OctoPrint",
				repo="OctoPrint-Pushbullet",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/OctoPrint/OctoPrint-Pushbullet/archive/{target_version}.zip"
			)
		)

	##~~ Internal utility methods

	def _send_periodic_update(self, progress):
		if not self._periodic_updates:
			return

		with self._periodic_updates_lock:
			# Check if we even are supposed to run
			if self._next_message is None:
				return

			# Check if enough time has passed since last message
			if time.time() < self._next_message:
				return

			self._next_message = time.time() + self._periodic_updates_interval

			current_data = self._printer.get_current_data()
			elapsed_time, remaining_time = self._get_progress_data(current_data)

			if elapsed_time is None:
				# doesn't really make sense to send a progress if we haven't properly started yet
				return

			if remaining_time is None:
				# also can't check if we need to send a report if time_left is None
				return

			# check if there is time for another message before job ends
			if remaining_time < self._periodic_updates_interval:
				self._logger.debug("Skip trailing message since print "
				                   "is nearly done: {} of {}".format(remaining_time,
				                                                     self._periodic_updates_interval))
				return

			path = current_data["job"]["file"]["path"]
			placeholders = dict(progress=progress,
			                    file=path,
			                    elapsed_time=_get_time_from_seconds(elapsed_time, default="?"),
			                    remaining_time=_get_time_from_seconds(remaining_time, default="?"),
			                    eta=_get_eta_from_seconds(remaining_time, default="?"))

			title = self._settings.get(["printProgress", "title"]).format(**placeholders)
			body = self._settings.get(["printProgress", "body"]).format(**placeholders)
			filename = _PERIODIC_FILENAME_FORMAT.format(name=os.path.splitext(path)[0],
			                                            progress=progress)

			self._send_message_with_webcam_image(title, body, filename=filename)

	def _get_snapshot_source(self):
		if get_snapshot_webcam is None: # OctoPrint < 1.9.0
			snapshot_url = self._settings.global_get(["webcam", "snapshot"])
			if not snapshot_url:
				return None

			def take_snapshot():
				response = requests.get(snapshot_url, verify=False, stream=True)
				response.raise_for_status()
				return response.iter_content(chunk_size=1024)

			return (take_snapshot,
			        self._settings.global_get_boolean(["webcam", "flipH"]),
			        self._settings.global_get_boolean(["webcam", "flipV"]),
			        self._settings.global_get_boolean(["webcam", "rotate90"]))

		webcam = get_snapshot_webcam()
		if webcam is None or not webcam.config.canSnapshot:
			return None

		return (lambda: webcam.providerPlugin.take_webcam_snapshot(webcam.config.name),
		        webcam.config.flipH,
		        webcam.config.flipV,
		        webcam.config.rotate90)

	def _send_message_with_webcam_image(self, title, body, filename=None, sender=None):
		if filename is None:
			filename = "test-{}.jpg".format("".join([random.choice(string.ascii_letters) for _ in range(16)]))

		if sender is None:
			sender = self._sender

		if not sender:
			return False

		snapshot_source = self._get_snapshot_source()
		if snapshot_source:
			take_snapshot, hflip, vflip, rotate = snapshot_source
			try:
				tempFile = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
				for chunk in take_snapshot():
					tempFile.write(chunk)
				tempFile.close()
			except Exception as e:
				self._logger.exception(
					"Exception while fetching snapshot from webcam, sending only a note: {message}".format(
						message=str(e)))
			else:
				# Flip or rotate as needed
				self._process_snapshot(tempFile.name, hflip, vflip, rotate)

				if self._send_file(sender, tempFile.name, filename, title + " " + body):
					return True
				self._logger.warning("Could not send a file message with the webcam image, sending only a note")

		return self._send_note(sender, title, body)

	def _send_note(self, sender, title, body):
		try:
			sender.push_note(title, body)
		except:
			self._logger.exception("Error while pushing a note")
			return False
		return True

	def _send_file(self, sender, path, filename, body):
		try:
			with open(path, "rb") as pic:
				try:
					file_data = self._bullet.upload_file(pic, filename)
				except Exception as e:
					self._logger.exception("Error while uploading snapshot, sending only a note: {}".format(str(e)))
					return False

			sender.push_file(file_data["file_name"], file_data["file_url"], file_data["file_type"], body=body)
			return True
		except Exception as e:
			self._logger.exception("Exception while uploading snapshot to Pushbullet, sending only a note: {message}".format(message=str(e)))
			return False
		finally:
			try:
				os.remove(path)
			except:
				self._logger.exception("Could not remove temporary snapshot file {}".format(path))

	def _create_sender(self, token, channel=None):
		try:
			bullet = pushbullet.PushBullet(token)
			sender = bullet

			# Setup channel object if channel setting is present
			if channel:
				for channel_obj in bullet.channels:
					if channel_obj.channel_tag == channel:
						sender = channel_obj
						self._logger.info("Connected to PushBullet on channel {}".format(channel))
						break
				else:
					self._logger.warning("Could not find channel {}, please check your configuration!".format(channel))
					raise NoSuchChannel(channel)

			self._logger.info("Connected to PushBullet")
			return bullet, sender
		except NoSuchChannel:
			raise
		except pushbullet.InvalidKeyError:
			raise
		except:
			self._logger.exception("Error while instantiating PushBullet")
			return None, None

	def _process_snapshot(self, snapshot_path, hflip, vflip, rotate):
		if not hflip and not vflip and not rotate:
			return

		try:
			with Image.open(snapshot_path) as image:
				processed = image
				if hflip:
					processed = ImageOps.mirror(processed)
				if vflip:
					processed = ImageOps.flip(processed)
				if rotate:
					processed = processed.rotate(90, expand=True)

				if processed.mode not in ("L", "RGB"):
					processed = processed.convert("RGB")

				processed.save(snapshot_path)
		except Exception:
			self._logger.exception("Could not rotate/flip the snapshot, sending it unprocessed")


class NoSuchChannel(Exception):
	def __init__(self, channel, *args, **kwargs):
		Exception.__init__(self, *args, **kwargs)
		self.channel = channel


__plugin_name__ = "Pushbullet"
def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PushbulletPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}

