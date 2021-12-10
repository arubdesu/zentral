from datetime import timedelta
import logging
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone
from zentral.conf import settings

logger = logging.getLogger("zentral.contrib.inventory.management.commands.cleanup_inventory_history")


DELETE_MACHINE_SNAPSHOT_COMMIT_QUERY = """
DELETE FROM inventory_machinesnapshotcommit AS msc
USING (
    SELECT serial_number, source_id,
           LEAST(MAX(created_at), timestamp with time zone %s) as max_created_at
    FROM inventory_machinesnapshotcommit
    GROUP BY serial_number, source_id
) AS msc_agg
WHERE
    msc.serial_number = msc_agg.serial_number
    AND msc.source_id = msc_agg.source_id
    AND msc.created_at < msc_agg.max_created_at;
"""


ORPHANS = (
    # MachineSnapshot of archived machines
    ("inventory_machinesnapshot", "id",
     (("machine_snapshot_id", "inventory_machinesnapshotcommit"),)),
    # PuppetNode
    ("inventory_puppetnode", "id",
     (("puppet_node_id", "inventory_machinesnapshot"),)),
    # PrincipalUser
    ("inventory_principaluser", "id",
     (("principal_user_id", "inventory_machinesnapshot"),)),
    # SystemInfo
    ("inventory_systeminfo", "id",
     (("system_info_id", "inventory_machinesnapshot"),)),
    # TeamViewer
    ("inventory_teamviewer", "id",
     (("teamviewer_id", "inventory_machinesnapshot"),)),
    # OSVersion
    ("inventory_osversion", "id",
     (("os_version_id", "inventory_machinesnapshot"),)),
    # DebPackage
    ("inventory_debpackage", "id",
     (("debpackage_id", "inventory_machinesnapshot_deb_packages"),)),
    # ProgramInstance
    ("inventory_programinstance", "id",
     (("programinstance_id", "inventory_machinesnapshot_program_instances"),)),
    # Program
    ("inventory_program", "id",
     (("program_id", "inventory_programinstance"),)),
    # MachineGroup
    ("inventory_machinegroup", "id",
     (("machinegroup_id", "inventory_machinesnapshot_groups"),)),
    # Link
    ("inventory_link", "id",
     (("link_id", "inventory_machinesnapshot_links"),
      ("link_id", "inventory_machinegroup_links"),
      ("link_id", "inventory_machinegroup_machine_links"),
      ("link_id", "inventory_businessunit_links"))),
    # Disks
    ("inventory_disk", "id",
     (("disk_id", "inventory_machinesnapshot_disks"),)),
    # NetworkInterface
    ("inventory_networkinterface", "id",
     (("networkinterface_id", "inventory_machinesnapshot_network_interfaces"),)),
    # OSXAppInstance
    ("inventory_osxappinstance", "id",
     (("osxappinstance_id", "inventory_machinesnapshot_osx_app_instances"),)),
    # Certificate
    ("inventory_certificate", "id",
     (("signed_by_id", "inventory_osxappinstance"),
      ("signed_by_id", "inventory_certificate"),
      ("signed_by_id", "inventory_file"),
      ("certificate_id", "inventory_machinesnapshot_certificates"))),
    # OSXApp
    ("inventory_osxapp", "id",
     (("app_id", "inventory_osxappinstance"),
      ("bundle_id", "inventory_file"))),
    # ProfilePayload for profiles not linked to machine snapshots
    ("inventory_profile_payloads", "profile_id",
     (("profile_id", "inventory_machinesnapshot_profiles"),)),
    # Payload not linked to profiles
    ("inventory_payload", "id",
     (("payload_id", "inventory_profile_payloads"),)),
    # Profile not linked to machine snapshots
    ("inventory_profile", "id",
     (("profile_id", "inventory_machinesnapshot_profiles"),)),
)


class Command(BaseCommand):
    help = "Cleanup inventory history"

    def add_arguments(self, parser):
        default_snapshot_retention_days = 30  # 30 days if absent
        try:
            default_snapshot_retention_days = int(
                settings['apps']['zentral.contrib.inventory']['snapshot_retention_days']
            )
        except KeyError:
            pass
        except (TypeError, ValueError):
            logger.error("Wrong value set snapshot_retention_days, default of %s used",
                         default_snapshot_retention_days)
        default_snapshot_retention_days = max(1, default_snapshot_retention_days)  # minimum 1 day
        parser.add_argument("-q", "--quiet", action="store_true", help="no output if no errors")
        parser.add_argument(
            '--days', type=int,
            default=default_snapshot_retention_days,
            help=f'number of days to keep, default {default_snapshot_retention_days}'
        )

    def set_options(self, **options):
        self.quiet = options.get("quiet", False)
        self.min_date = timezone.now() - timedelta(days=options["days"])

    def handle(self, *args, **kwargs):
        self.set_options(**kwargs)
        with transaction.atomic():
            with connection.cursor() as cursor:
                self.cleanup_inventory_history(cursor)

    def cleanup_inventory_history(self, cursor):
        if not self.quiet:
            print("min date", self.min_date.isoformat())
        # delete older machine snapshot commits
        cursor.execute(DELETE_MACHINE_SNAPSHOT_COMMIT_QUERY, [self.min_date])
        if not self.quiet:
            print("machine snapshot commits", cursor.rowcount)

        # orphans
        for table_name, attribute, links in ORPHANS:
            wheres = []
            for fk_attribute, fk_table in links:
                wheres.append("{} NOT IN (SELECT DISTINCT {} FROM {} WHERE {} IS NOT NULL)".format(
                              attribute, fk_attribute, fk_table, fk_attribute))
            query = "DELETE FROM {} WHERE {}".format(table_name, " AND ".join(wheres))
            cursor.execute(query)
            if not self.quiet:
                print(table_name, cursor.rowcount)
