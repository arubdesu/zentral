import logging
from django.db import transaction
from django.urls import reverse
from zentral.conf import settings
from zentral.contrib.mdm.models import ArtifactOperation, Channel, DeviceArtifact, Platform, TargetArtifactStatus
from zentral.contrib.mdm.tasks import send_enrolled_device_notification
from .base import register_command, Command
from .installed_application_list import InstalledApplicationList


logger = logging.getLogger("zentral.contrib.mdm.commands.install_enterprise_application")


class InstallEnterpriseApplication(Command):
    request_type = "InstallEnterpriseApplication"
    allowed_channel = Channel.Device
    allowed_platform = Platform.macOS
    allowed_in_user_enrollment = True
    artifact_operation = ArtifactOperation.Installation

    def build_command(self):
        # TODO manage options
        # see https://developer.apple.com/documentation/devicemanagement/installenterpriseapplicationcommand/command
        manifest = self.artifact_version.enterprise_app.manifest
        manifest["items"][0]["assets"][0]["url"] = "https://{}{}".format(settings["api"]["fqdn"],
                                                                         reverse("mdm:enterprise_app_download",
                                                                                 args=(self.db_command.uuid,)))
        return {
            # TODO manage options
            # App must install to /Applications
            # App must contain a single app
            "InstallAsManaged": True,
            "ChangeManagementState": "Managed",
            "ManagementFlags": 1,
            "Manifest": manifest
        }

    def command_acknowledged(self):
        if self.artifact_version.enterprise_app.bundles:
            device_artifact, created = DeviceArtifact.objects.update_or_create(
                enrolled_device=self.enrolled_device,
                artifact_version=self.artifact_version,
                defaults={"status": TargetArtifactStatus.AwaitingConfirmation.name}
            )
            # queue an installed application list command
            first_delay_seconds = 15  # TODO hardcoded
            InstalledApplicationList.create_for_device(
                self.enrolled_device,
                self.artifact_version,
                queue=True, delay=first_delay_seconds
            )
            transaction.on_commit(lambda: send_enrolled_device_notification(self.enrolled_device,
                                                                            delay=first_delay_seconds))
        else:
            # cleanup
            (DeviceArtifact.objects.filter(enrolled_device=self.enrolled_device,
                                           artifact_version__artifact=self.artifact)
                                   .exclude(artifact_version=self.artifact_version)
                                   .delete())
            # update or create new record
            DeviceArtifact.objects.update_or_create(
                enrolled_device=self.enrolled_device,
                artifact_version=self.artifact_version,
                defaults={"status": TargetArtifactStatus.Acknowledged.name}
            )


register_command(InstallEnterpriseApplication)
