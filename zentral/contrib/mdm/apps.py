from zentral.utils.apps import ZentralAppConfig


class ZentralMDMAppConfig(ZentralAppConfig):
    name = "zentral.contrib.mdm"
    verbose_name = "Zentral MDM contrib app"
    permission_models = (
        "artifact",
        "blueprint",
        "depdevice",
        "depenrollment",
        "depvirtualserver",
        "deviceartifact",
        "enrolleddevice",
        "enrolleduser",
        "enterpriseapp",
        "profile",
        "pushcertificate",
        "otaenrollment",
        "scepconfig",
        "userartifact",
        "userenrollment",
    )
