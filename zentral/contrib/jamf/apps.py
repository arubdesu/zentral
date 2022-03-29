from zentral.utils.apps import ZentralAppConfig


class ZentralJamfAppConfig(ZentralAppConfig):
    name = "zentral.contrib.jamf"
    default = True
    verbose_name = "Zentral jamf contrib app"
    permission_models = ("jamfinstance", "tagconfig")
