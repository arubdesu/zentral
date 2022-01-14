from datetime import datetime, timedelta
from itertools import chain
import logging
import os.path
import plistlib
import re
import unicodedata
import urllib.parse
from django.contrib.postgres.fields import ArrayField, JSONField
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, connection
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.text import slugify
from zentral.conf import settings
from zentral.contrib.inventory.models import BaseEnrollment, MetaBusinessUnit, Tag
from zentral.utils.text import get_version_sort_key
from .conf import monolith_conf
from .utils import build_manifest_enrollment_package, make_printer_package_info


logger = logging.getLogger("zentral.contrib.monolith.models")


# PkgInfo / Catalog / Manifest


class MunkiNameError(Exception):
    pass


def build_munki_name(model, key, name, ext=None):
    # first, the model
    elements = [model.replace("_", "-")]

    # then, the primary keys
    if isinstance(key, list):
        key = "-".join(str(int(pk)) for pk in key)
    else:
        key = str(int(key))
    elements.append(key)

    # then, a meaningful name
    # to ascii
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    # make it a slug, preserve some common separators
    name = re.sub(r'[^\w\s\._-]', '', name).strip()
    # replace all common separators by -
    name = re.sub(r'[\s\._-]+', '-', name).strip("-")
    elements.append(name or "-")

    # an eventual file extension
    if ext:
        elements.append(ext.replace(".", ""))

    return ".".join(elements)


def parse_munki_name(name):
    try:
        model, key, _ = name.split(".", 2)
        model = model.replace("-", "_")
        if "-" in key:
            key = [int(pk) for pk in key.split("-")]
        else:
            key = int(key)
        return model, key
    except (TypeError, ValueError):
        raise MunkiNameError


class Catalog(models.Model):
    name = models.CharField(max_length=256, unique=True)
    priority = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ('-archived_at', '-priority', 'name')

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("monolith:catalog", args=(self.pk,))

    def get_pkg_info_url(self):
        return "{}?{}".format(reverse("monolith:pkg_infos"),
                              urllib.parse.urlencode({"catalog": self.pk}))

    def can_be_deleted(self):
        return (monolith_conf.repository.manual_catalog_management
                and self.pkginfo_set.filter(archived_at__isnull=True).count() == 0
                and self.manifestcatalog_set.count() == 0)


class PkgInfoCategory(models.Model):
    name = models.CharField(max_length=256, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class PkgInfoName(models.Model):
    name = models.CharField(max_length=256, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name

    def active_pkginfos(self):
        return self.pkginfo_set.filter(archived_at__isnull=True)


class PkgInfoManager(models.Manager):
    def alles(self, **kwargs):
        params = []
        # first we aggregate the package info, with the munki managed installs
        aggregated_pi_query = (
            "select pn.id as pn_pk, pn.name, pi.id as pi_pk, pi.version, pi.data -> 'zentral_monolith' as pi_opts,"
            "count(mi.*), sum(count(mi.*)) over (partition by pn.id) as pn_total "
            "from monolith_pkginfoname as pn "
            "join monolith_pkginfo as pi on (pi.name_id = pn.id) "
            "left join munki_managedinstall as mi on "
            "(pn.name = mi.name and pi.version = mi.installed_version) "
            "where pi.archived_at is null"
        )
        name = kwargs.get("name")
        if name:
            params.append("%{}%".format(connection.ops.prep_for_like_query(name)))
            aggregated_pi_query += " and UPPER(pn.name) LIKE UPPER(%s)"
        name_id = kwargs.get("name_id")
        if name_id:
            params.append(name_id)
            aggregated_pi_query += " and pn.id = %s"
        aggregated_pi_query += " group by pn.id, pn.name, pi.id, pi.version, pi.data"

        # then we build the full query
        # join and aggregate the catalogs, compte the percentages
        query = (
            f"with aggregated_pi as ({aggregated_pi_query}) "
            "select api.*,"
            "case when pn_total=0 then null else 100.0 * count / pn_total end as percent,"
            "json_agg(distinct jsonb_build_object('pk', c.id, 'name', c.name, 'priority', c.priority)) as catalogs "
            "from aggregated_pi as api "
            "join monolith_pkginfo_catalogs as pc on (pc.pkginfo_id = api.pi_pk) "
            "join monolith_catalog as c on (c.id = pc.catalog_id) "
            "where c.archived_at is null "
        )
        catalog = kwargs.get("catalog")
        if catalog:
            params.append(catalog.id)
            query += " and c.id = %s"
        query += (
            " group by api.pn_pk, api.name, api.pi_pk, api.version, api.pi_opts, api.count, api.pn_total "
            "order by api.name, api.pn_pk, api.pi_pk"
        )

        # run the query and organize the results
        cursor = connection.cursor()
        cursor.execute(query, params)
        current_pn = current_pn_pk = None
        name_c = info_c = 0
        pkg_name_list = []
        seen_tag_names = set([])
        pi_opts_with_tags = []
        for pn_pk, pn_name, pi_pk, version, pi_opts, count, pn_total, percent, catalogs in cursor.fetchall():
            info_c += 1
            pi = {'pk': pi_pk,
                  'version': version,
                  'version_sort': get_version_sort_key(version),
                  'catalogs': sorted(catalogs, key=lambda c: (c["priority"], c["name"])),
                  'count': int(count),
                  'percent': percent}
            if pi_opts:
                pi['options'] = pi_opts
                excluded_tags = pi_opts.get("excluded_tags")
                shards = pi_opts.setdefault("shards", {})
                modulo = shards.setdefault("modulo", 100)
                shards.setdefault("default", modulo)
                tag_shards = shards.get("tags")
                if isinstance(excluded_tags, list) or isinstance(tag_shards, dict):
                    if excluded_tags:
                        seen_tag_names.update(excluded_tags)
                    if tag_shards:
                        seen_tag_names.update(tag_shards.keys())
                    pi_opts_with_tags.append(pi_opts)
            if pn_pk != current_pn_pk:
                if current_pn is not None:
                    current_pn['pkg_infos'].sort(key=lambda pi: pi["version_sort"], reverse=True)
                    pkg_name_list.append(current_pn)
                    name_c += 1
                current_pn_pk = pn_pk
                current_pn = {'id': pn_pk,
                              'name': pn_name,
                              'count': int(pn_total),
                              'pkg_infos': []}
            current_pn['pkg_infos'].append(pi)
        if current_pn:
            current_pn['pkg_infos'].sort(key=lambda pi: pi["version_sort"], reverse=True)
            pkg_name_list.append(current_pn)
            name_c += 1
        if seen_tag_names:
            # rehydrate the tags
            seen_tags = {
                tag.name: tag
                for tag in Tag.objects.select_related("meta_business_unit", "taxonomy").filter(name__in=seen_tag_names)
            }
            for pi_opts in pi_opts_with_tags:
                excluded_tags = pi_opts.pop("excluded_tags", None)
                if isinstance(excluded_tags, list):
                    pi_opts["excluded_tags"] = [
                        seen_tags[tag_name]
                        for tag_name in sorted(excluded_tags)
                        if tag_name in seen_tags
                    ]
                shards = pi_opts["shards"]
                tag_shards = shards.pop("tags", None)
                if isinstance(tag_shards, dict):
                    pi_opts["shards"]["tags"] = [
                        (seen_tags[tag_name], shard)
                        for tag_name, shard in sorted(tag_shards.items(), key=lambda t:t[0].lower())
                        if tag_name in seen_tags
                    ]
        return name_c, info_c, pkg_name_list


class PkgInfo(models.Model):
    name = models.ForeignKey(PkgInfoName, on_delete=models.PROTECT)
    version = models.CharField(max_length=256)
    catalogs = models.ManyToManyField(Catalog)
    category = models.ForeignKey(PkgInfoCategory, on_delete=models.SET_NULL, null=True, blank=True)
    requires = models.ManyToManyField(PkgInfoName, related_name="required_by")
    update_for = models.ManyToManyField(PkgInfoName, related_name="updated_by")
    data = JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(blank=True, null=True)

    objects = PkgInfoManager()

    class Meta:
        ordering = ('name', 'version')
        unique_together = (('name', 'version'),)

    def get_key(self):
        return (self.name.name, self.version)

    def __str__(self):
        return "{} {}".format(self.name, self.version)

    def active_catalogs(self):
        return self.catalogs.filter(archived_at__isnull=True)

    def get_pkg_info(self):
        pkg_info = self.data.copy()
        pkg_info.pop("catalogs", None)
        for attr in ("installer_item_location", "uninstaller_item_loc"):
            loc = pkg_info.pop(attr, None)
            if loc:
                root, ext = os.path.splitext(loc)
                name = os.path.basename(root)
                pkg_info[attr] = build_munki_name("repository_package", self.id, name, ext)
        return pkg_info

    def get_absolute_url(self):
        return "{}#{}".format(reverse("monolith:pkg_info_name", args=(self.name.id,)), self.pk)


SUB_MANIFEST_PKG_INFO_KEY_CHOICES = (
    ('managed_installs', 'Managed Installs'),
    ('managed_uninstalls', 'Managed Uninstalls'),
    ('optional_installs', 'Optional Installs'),
    ('managed_updates', 'Managed Updates'),
)


class SubManifest(models.Model):
    """Group of pkginfo or attachments (pkgs, cfg profiles, scripts)."""

    # to restrict some sub manifests to a MBU
    meta_business_unit = models.ForeignKey(MetaBusinessUnit, on_delete=models.SET_NULL, blank=True, null=True)

    name = models.CharField(max_length=256)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        if self.meta_business_unit:
            return "{} / {}".format(self.meta_business_unit.name, self.name)
        else:
            return self.name

    def get_absolute_url(self):
        return reverse('monolith:sub_manifest', args=(self.pk,))

    def has_attachments(self):
        return SubManifestAttachment.objects.filter(sub_manifest=self).count() > 0

    def pkg_info_dict(self, include_trashed_attachments=False):
        pkg_info_d = {'keys': {},
                      'total': {'pkginfo': 0}}
        for sma_type in SUB_MANIFEST_ATTACHMENT_TYPES:
            pkg_info_d['total'][sma_type] = 0
        for smpi in self.submanifestpkginfo_set.select_related('pkg_info_name', 'condition'):
            key_dict = pkg_info_d['keys'].setdefault(smpi.key,
                                                     {'key_display': smpi.get_key_display(),
                                                      'key_list': []})
            key_dict['key_list'].append((smpi.pkg_info_name.name, smpi))
            pkg_info_d['total']['pkginfo'] += 1
        if not include_trashed_attachments:
            sma_qs = SubManifestAttachment.objects.active()
        else:
            sma_qs = SubManifestAttachment.objects.all()
        for sma in sma_qs.select_related('condition').filter(sub_manifest=self):
            key_dict = pkg_info_d['keys'].setdefault(sma.key,
                                                     {'key_display': sma.get_key_display(),
                                                      'key_list': []})
            key_dict['key_list'].append((sma.name, sma))
            pkg_info_d['total'][sma.type] += 1
        for key, key_d in pkg_info_d['keys'].items():
            key_d['key_list'].sort(key=lambda t: (t[0], -1 * t[1].pk))
        return pkg_info_d

    def get_munki_name(self):
        return build_munki_name("sub_manifest", self.id, self.name)

    def build(self):
        condition_d = {}
        featured_items = set()
        included_sma_names = set()
        for key, key_d in self.pkg_info_dict()['keys'].items():
            for _, smo in key_d['key_list']:
                if smo.condition:
                    condition = smo.condition.predicate
                else:
                    condition = None
                name = smo.get_name()
                if isinstance(smo, SubManifestPkgInfo):
                    options = smo.options
                else:
                    options = None
                if key in ('managed_installs', 'optional_installs'):
                    val = (name, options)
                else:
                    val = name
                condition_d.setdefault(condition, {}).setdefault(key, []).append(val)
                if key != "managed_uninstalls" and smo.featured_item:
                    featured_items.add(name)
                if isinstance(smo, SubManifestAttachment):
                    included_sma_names.add(smo.name)
        data = {}
        if featured_items:
            data["featured_items"] = sorted(featured_items)
        for condition, condition_key_d in condition_d.items():
            if condition is None:
                data.update(condition_key_d)
            else:
                condition_key_d["condition"] = condition
                data.setdefault("conditional_items", []).append(condition_key_d)
        # force uninstall on the not included attachments
        qs = SubManifestAttachment.objects.filter(sub_manifest=self).exclude(name__in=included_sma_names)
        data.setdefault('managed_uninstalls', []).extend({sma.get_name() for sma in qs})
        return data

    def can_be_deleted(self):
        return self.manifestsubmanifest_set.all().count() == 0

    def manifests_with_tags(self):
        mwt = []
        for msn in (self.manifestsubmanifest_set.select_related("manifest__meta_business_unit")
                                                .prefetch_related("tags")
                                                .all()):
            mwt.append((msn.tags.all(), msn.manifest))
        return mwt


class Condition(models.Model):
    name = models.CharField(max_length=256, unique=True)
    predicate = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("monolith:condition", args=(self.pk,))

    def can_be_deleted(self):
        return not self.submanifestpkginfo_set.count() and not self.submanifestattachment_set.count()

    def manifests(self):
        return Manifest.objects.distinct().filter(
            Q(manifestsubmanifest__sub_manifest__submanifestpkginfo__condition=self)
            | Q(manifestsubmanifest__sub_manifest__submanifestattachment__condition=self)
        )


class SubManifestPkgInfo(models.Model):
    sub_manifest = models.ForeignKey(SubManifest, on_delete=models.CASCADE)
    key = models.CharField(max_length=32, choices=SUB_MANIFEST_PKG_INFO_KEY_CHOICES)
    pkg_info_name = models.ForeignKey(PkgInfoName, on_delete=models.PROTECT)
    featured_item = models.BooleanField(default=False)
    condition = models.ForeignKey(Condition, on_delete=models.PROTECT, null=True, blank=True)
    options = JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('pkg_info_name',)
        unique_together = (('sub_manifest', 'pkg_info_name'),)

    def get_absolute_url(self):
        return "{}#smp_{}".format(reverse('monolith:sub_manifest', args=(self.sub_manifest.pk,)), self.pk)

    def get_name(self):
        return self.pkg_info_name.name

    # options

    @cached_property
    def excluded_tags(self):
        excluded_tag_names = self.options.get("excluded_tags")
        if not excluded_tag_names:
            return []
        return Tag.objects.select_related("meta_business_unit", "taxonomy").filter(name__in=excluded_tag_names)

    @cached_property
    def tag_shards(self):
        tag_shards = self.options.get("shards", {}).get("tags")
        if not tag_shards:
            return {}
        tags = (
            Tag.objects.select_related("meta_business_unit", "taxonomy")
                       .filter(name__in=tag_shards.keys())
                       .order_by("name")
        )
        return [(tag, tag_shards[tag.name]) for tag in tags]

    @property
    def default_shard(self):
        return self.options.get("shards", {}).get("default", 100)

    @property
    def shard_modulo(self):
        return self.options.get("shards", {}).get("modulo", 100)


def attachment_path(instance, filename):
    # TODO overflow ?
    return 'monolith/sub_manifests/{0:08d}/{1}/{2}_{3:04d}'.format(
        instance.sub_manifest.id,
        instance.type,
        instance.name,
        instance.version
    )


SUB_MANIFEST_ATTACHMENT_TYPES = {
    "configuration_profile": {'name': 'configuration profile',
                              'extension': '.mobileconfig'},
    "package": {'name': 'package',
                'extension': '.pkg'},
    "script": {'name': 'script'},
}

SUB_MANIFEST_ATTACHMENT_TYPE_CHOICES = [
    (k, v['name']) for k, v in SUB_MANIFEST_ATTACHMENT_TYPES.items()
]


class SubManifestAttachmentManager(models.Manager):
    def newest(self):
        return self.order_by('name', '-version').distinct('name')

    def active(self):
        return self.newest().filter(trashed_at__isnull=True)

    def trash(self, sub_manifest, name):
        for sma in self.filter(sub_manifest=sub_manifest, name=name):
            sma.mark_as_trashed()


class SubManifestAttachment(models.Model):
    sub_manifest = models.ForeignKey(SubManifest, on_delete=models.CASCADE)
    key = models.CharField(max_length=32, choices=SUB_MANIFEST_PKG_INFO_KEY_CHOICES)
    type = models.CharField(max_length=32, choices=SUB_MANIFEST_ATTACHMENT_TYPE_CHOICES)
    name = models.CharField(max_length=256)
    featured_item = models.BooleanField(default=False)
    condition = models.ForeignKey(Condition, on_delete=models.PROTECT, null=True, blank=True)
    identifier = models.TextField(blank=True, null=True)
    version = models.PositiveSmallIntegerField(default=0)
    file = models.FileField(upload_to=attachment_path, blank=True)
    pkg_info = JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    trashed_at = models.DateTimeField(null=True)

    objects = SubManifestAttachmentManager()

    class Meta:
        unique_together = (('sub_manifest', 'name', 'version'),)

    def get_absolute_url(self):
        return "{}#sma_{}".format(reverse('monolith:sub_manifest', args=(self.sub_manifest.pk,)), self.pk)

    def get_name(self):
        # remove the "-" from the name, because munki thinks it is a separator for a version number,
        # and will split it to get the name of the package info, before searching for it in the catalogs.
        name = re.sub(r'-+', '_', self.name)
        return "sub manifest {} {} {}".format(self.sub_manifest.id,
                                              self.get_type_display(),
                                              name)

    def can_be_downloaded(self):
        return self.type in ("package", "configuration_profile")

    def get_download_name(self):
        if self.can_be_downloaded():
            return build_munki_name(
                "sub_manifest_attachment",
                [self.sub_manifest.id, self.id],
                self.name,
                SUB_MANIFEST_ATTACHMENT_TYPES[self.type]['extension']
            )

    def get_pkg_info(self):
        pkg_info = self.pkg_info.copy()
        pkg_info['name'] = self.get_name()
        if self.can_be_downloaded():
            pkg_info['installer_item_location'] = self.get_download_name()
        return pkg_info

    def get_content_type(self):
        if self.type == "package":
            return "application/octet-stream"
        elif self.type == "configuration_profile":
            return "application/x-apple-aspen-config"

    def mark_as_trashed(self):
        self.trashed_at = datetime.now()
        self.save()


class Manifest(models.Model):
    meta_business_unit = models.ForeignKey(MetaBusinessUnit, on_delete=models.PROTECT)
    name = models.CharField(max_length=256)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name', 'meta_business_unit__name',)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('monolith:manifest', args=(self.pk,))

    def bump_version(self):
        self.version = F("version") + 1
        self.save()

    def catalogs(self, tags=None):
        if tags is None:
            tags = []
        return [mc.catalog
                for mc in (self.manifestcatalog_set
                               .distinct()
                               .select_related("catalog")
                               .filter(Q(tags__isnull=True) | Q(tags__in=tags)))]

    def sub_manifests(self, tags=None):
        if tags is None:
            tags = []
        return [msm.sub_manifest
                for msm in (self.manifestsubmanifest_set
                                .distinct()
                                .select_related("sub_manifest")
                                .filter(Q(tags__isnull=True) | Q(tags__in=tags)))]

    def sub_manifest(self, sm_id, tags=None):
        if tags is None:
            tags = []
        try:
            msm = (self.manifestsubmanifest_set
                       .distinct()
                       .select_related("sub_manifest")
                       .filter(Q(tags__isnull=True) | Q(tags__in=tags))
                       .get(sub_manifest__id=sm_id))
        except ManifestSubManifest.DoesNotExist:
            pass
        else:
            return msm.sub_manifest

    def enrollment_packages(self, tags=None):
        # Find the existing enrollment packages for a given set of tags.
        # For each builder, we select the enrollment package whose tags
        # are a subset of the machine tags.
        # In case of conflicts, we return the first builder found.
        tags = set(tags or [])
        d = {}
        for ep in (self.manifestenrollmentpackage_set
                       .prefetch_related("tags")
                       .filter(builder__in=monolith_conf.enrollment_package_builders.keys())
                       .filter(Q(tags__isnull=True) | Q(tags__in=tags))
                       .order_by("id")):
            if ep.tag_set.issubset(tags):
                found_ep = d.get(ep.builder)
                if found_ep:
                    if len(found_ep.tag_set) < len(ep.tag_set):
                        d[ep.builder] = ep
                    elif len(found_ep.tag_set) == len(ep.tag_set):
                        # TODO: conflict!
                        logger.error("Manifest %s builder %s mep conflict for machine tags %s",
                                     self.id, ep.builder, ", ".join(str(t) for t in tags))
                else:
                    d[ep.builder] = ep
        return d

    def printers(self, tags=None):
        # Find the existing printers for a given set of tags.
        if tags is None:
            tags = []
        qs = (self.printer_set
                  .select_related("required_package")
                  .prefetch_related("tags")
                  .filter(Q(tags__isnull=True) | Q(tags__in=tags))
                  .filter(trashed_at__isnull=True))
        return list(qs)

    def pkginfos_with_deps_and_updates(self, tags=None):
        """PkgInfos linked to a manifest for a given set of tags"""
        if tags:
            m2mt_filter = "OR m2mt.tag_id in ({})".format(",".join(str(int(t.id)) for t in tags))
        else:
            m2mt_filter = ""
        query = (
            "WITH RECURSIVE pkginfos_with_deps_and_updates AS ( "
            "SELECT pi.id as pi_id, pi.version as pi_version, pn.id AS pn_id, pn.name as pn_name "
            "FROM monolith_pkginfo pi "
            "JOIN monolith_pkginfoname pn ON (pi.name_id=pn.id) "
            "JOIN monolith_submanifestpkginfo sm ON (pn.id=pkg_info_name_id) "
            "JOIN monolith_manifestsubmanifest ms ON (sm.sub_manifest_id=ms.sub_manifest_id) "
            "LEFT JOIN monolith_manifestsubmanifest_tags m2mt ON (ms.id=m2mt.manifestsubmanifest_id) "
            "WHERE ms.manifest_id = {manifest_id} "
            "AND (m2mt.tag_id IS NULL {m2mt_filter}) "
            "UNION "
            "SELECT pi.id, pi.version, pn.id, pn.name "
            "FROM monolith_pkginfo pi "
            "JOIN monolith_pkginfoname pn ON (pi.name_id=pn.id) "
            "LEFT JOIN monolith_pkginfo_requires pr ON (pr.pkginfoname_id=pn.id) "
            "LEFT JOIN monolith_pkginfo_update_for pu ON (pu.pkginfo_id=pi.id) "
            "JOIN pkginfos_with_deps_and_updates rec ON (pr.pkginfo_id=rec.pi_id OR pu.pkginfoname_id=rec.pn_id) "
            ") "
            "SELECT pi_id as id, pi_version as version from pkginfos_with_deps_and_updates "
            "JOIN monolith_pkginfo_catalogs pc ON (pi_id=pc.pkginfo_id) "
            "JOIN monolith_manifestcatalog mc ON (pc.catalog_id=mc.catalog_id) "
            "LEFT JOIN monolith_manifestcatalog_tags m2mt ON (mc.id=m2mt.manifestcatalog_id) "
            "WHERE mc.manifest_id = {manifest_id} "
            "AND (m2mt.tag_id IS NULL {m2mt_filter});"
        ).format(manifest_id=int(self.id), m2mt_filter=m2mt_filter)
        return PkgInfo.objects.raw(query)

    def _pkginfo_deps_and_updates(self, package_names, tags):
        package_names = ",".join("'{}'".format(package_name)
                                 for package_name in set(package_names))
        if not package_names:
            return PkgInfo.objects.none()
        if tags:
            m2mt_filter = "OR m2mt.tag_id in ({})".format(",".join(str(int(t.id)) for t in tags))
        else:
            m2mt_filter = ""
        query = (
            "WITH RECURSIVE pkginfos_with_deps_and_updates AS ( "
            "SELECT pi.id as pi_id, pi.version as pi_version, pn.id AS pn_id, pn.name as pn_name "
            "FROM monolith_pkginfo pi "
            "JOIN monolith_pkginfoname pn ON (pi.name_id=pn.id) "
            "WHERE pn.name in ({package_names}) "
            "UNION "
            "SELECT pi.id, pi.version, pn.id, pn.name "
            "FROM monolith_pkginfo pi "
            "JOIN monolith_pkginfoname pn ON (pi.name_id=pn.id) "
            "LEFT JOIN monolith_pkginfo_requires pr ON (pr.pkginfoname_id=pn.id) "
            "LEFT JOIN monolith_pkginfo_update_for pu ON (pu.pkginfo_id=pi.id) "
            "JOIN pkginfos_with_deps_and_updates rec ON (pr.pkginfo_id=rec.pi_id OR pu.pkginfoname_id=rec.pn_id) "
            ") "
            "SELECT pi_id as id, pi_version as version from pkginfos_with_deps_and_updates "
            "JOIN monolith_pkginfo_catalogs pc ON (pi_id=pc.pkginfo_id) "
            "JOIN monolith_manifestcatalog mc ON (pc.catalog_id=mc.catalog_id) "
            "LEFT JOIN monolith_manifestcatalog_tags m2mt ON (mc.id=m2mt.manifestcatalog_id) "
            "WHERE mc.manifest_id = {manifest_id} "
            "AND (m2mt.tag_id IS NULL {m2mt_filter});"
        ).format(package_names=package_names, manifest_id=int(self.id), m2mt_filter=m2mt_filter)
        return PkgInfo.objects.raw(query)

    def enrollment_packages_pkginfo_deps(self, tags=None):
        """PkgInfos that enrollment packages require, with their dependencies"""
        required_packages_iter = chain.from_iterable(ep.get_requires()
                                                     for ep in self.enrollment_packages(tags).values())
        return self._pkginfo_deps_and_updates(required_packages_iter, tags)

    def printers_pkginfo_deps(self, tags=None):
        """PkgInfos that printers require, with their dependencies"""
        required_packages_gen = (p.required_package.name
                                 for p in self.printers(tags)
                                 if p.required_package)
        return self._pkginfo_deps_and_updates(required_packages_gen, tags)

    def default_managed_installs_deps(self, tags=None):
        """PkgInfos installed per default, with their dependencies"""
        return self._pkginfo_deps_and_updates(monolith_conf.get_default_managed_installs(), tags)

    # the manifest catalog - for a given set of tags

    def get_catalog_munki_name(self):
        return build_munki_name("manifest_catalog", self.pk, self.name)

    def build_catalog(self, tags=None):
        pkginfo_list = []

        # the repository catalogs pkginfos
        for pkginfo in (PkgInfo.objects.distinct()
                                       .select_related("name")
                                       .filter(archived_at__isnull=True,
                                               catalogs__in=self.catalogs(tags))):
            pkginfo_list.append(pkginfo.get_pkg_info())

        # the sub manifests attachments
        for sma in SubManifestAttachment.objects.newest().filter(sub_manifest__in=self.sub_manifests(tags)):
            pkginfo_list.append(sma.get_pkg_info())

        # the enrollment packages

        # add the unique selected enrollment package in scope for each builder
        in_scope_mep_builders = []
        for builder, enrollment_package in self.enrollment_packages(tags).items():
            in_scope_mep_builders.append(builder)
            pkginfo_list.append(enrollment_package.get_pkg_info())

        # add all the enrollment packages, for the builders not in scope, to allow removal
        not_in_scope_mep_qs = self.manifestenrollmentpackage_set.all()
        if in_scope_mep_builders:
            not_in_scope_mep_qs = not_in_scope_mep_qs.exclude(builder__in=in_scope_mep_builders)
        for not_in_scope_mep in not_in_scope_mep_qs:
            pkginfo_list.append(not_in_scope_mep.get_pkg_info())

        # include the catalog with all the printers for autoremove
        for printer in self.printer_set.all():
            pkginfo_list.append(printer.pkg_info)

        return pkginfo_list

    # the manifest

    def build(self, tags):
        data = {'catalogs': [self.get_catalog_munki_name()],
                'included_manifests': []}

        # include the sub manifests
        for sm in self.sub_manifests(tags):
            data['included_manifests'].append(sm.get_munki_name())

        # add default managed installs
        data['managed_installs'] = monolith_conf.get_default_managed_installs()

        # loop on the configured enrollment package builders
        enrollment_packages = self.enrollment_packages(tags)
        for mep in self.manifestenrollmentpackage_set.all():
            mep_name = mep.get_name()
            if mep.builder in enrollment_packages:
                if mep_name not in data['managed_installs']:
                    data['managed_installs'].append(mep_name)
            else:
                managed_uninstalls = data.setdefault('managed_uninstalls', [])
                if mep_name not in managed_uninstalls:
                    managed_uninstalls.append(mep_name)

        # include only the matching active printers as managed installs
        for printer in self.printers(tags):
            data.setdefault("managed_installs", []).append(printer.get_pkg_info_name())
        return data

    def serialize(self, tags):
        return plistlib.dumps(self.build(tags))


class ManifestCatalog(models.Model):
    manifest = models.ForeignKey(Manifest, on_delete=models.CASCADE)
    catalog = models.ForeignKey(Catalog, on_delete=models.PROTECT)
    tags = models.ManyToManyField(Tag)

    class Meta:
        ordering = ('-catalog__priority', '-catalog__name')


class ManifestSubManifest(models.Model):
    manifest = models.ForeignKey(Manifest, on_delete=models.CASCADE)
    sub_manifest = models.ForeignKey(SubManifest, on_delete=models.PROTECT)
    tags = models.ManyToManyField(Tag)


def enrollment_package_path(instance, filename):
    # TODO overflow ?
    return 'monolith/manifests/{0:08d}/enrollment_packages/{1}'.format(
        instance.manifest.id,
        filename
    )


class ManifestEnrollmentPackage(models.Model):
    manifest = models.ForeignKey(Manifest, on_delete=models.CASCADE)
    tags = models.ManyToManyField(Tag)

    builder = models.CharField(max_length=256)
    enrollment_pk = models.PositiveIntegerField(null=True)

    file = models.FileField(upload_to=enrollment_package_path, blank=True)
    pkg_info = JSONField(blank=True, null=True)
    version = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def delete(self, *args, **kwargs):
        self.file.delete(save=False)
        enrollment = self.get_enrollment()
        super().delete(*args, **kwargs)
        if enrollment:
            enrollment.delete()

    def get_installer_item_filename(self):
        return "{}.pkg".format(slugify("{} pk{} v{}".format(self.get_name(),
                                                            self.id,
                                                            self.version)))

    @cached_property
    def builder_class(self):
        return monolith_conf.enrollment_package_builders[self.builder]["class"]

    def get_name(self):
        return self.builder_class.name

    def get_requires(self):
        return monolith_conf.enrollment_package_builders[self.builder]["requires"]

    def get_pkg_info(self):
        pkg_info = self.pkg_info.copy()
        pkg_info["installer_item_location"] = build_munki_name("enrollment_pkg", self.id, self.get_name(), "pkg")
        return pkg_info

    @cached_property
    def tag_set(self):
        return set(self.tags.all())

    def get_enrollment(self):
        try:
            enrollment_model = self.builder_class.form.Meta.model
            return enrollment_model.objects.get(pk=self.enrollment_pk)
        except (AttributeError, ObjectDoesNotExist):
            pass

    def enrollment_update_callback(self):
        self.version = F("version") + 1
        self.save()
        self.refresh_from_db()
        build_manifest_enrollment_package(self)
        self.manifest.bump_version()

    def get_description_for_enrollment(self):
        return "Monolith manifest: {}".format(self.manifest)

    def get_absolute_url(self):
        return "{}#mep_{}".format(reverse("monolith:manifest", args=(self.manifest.pk,)), self.pk)

    def serialize_for_event(self):
        """used for the enrollment secret verification events, via the enrollment"""
        return {"monolith_manifest_enrollment_package": {"pk": self.pk,
                                                         "manifest": {"pk": self.manifest.pk,
                                                                      "name": str(self.manifest)}}}


# Cache server


class CacheServerManager(models.Manager):
    def get_current_for_manifest(self, manifest, max_age):
        min_updated_at = timezone.now() - timedelta(seconds=max_age)
        return self.filter(manifest=manifest,
                           updated_at__gte=min_updated_at)


class CacheServer(models.Model):
    name = models.CharField(max_length=256)
    manifest = models.ForeignKey(Manifest, on_delete=models.CASCADE)
    public_ip_address = models.GenericIPAddressField()
    base_url = models.URLField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CacheServerManager()

    class Meta:
        unique_together = (("name", "manifest"),)

    def get_cache_url(self, url):
        """Build the cache url to redirect to from the repo url

        Apply the scheme and netloc from the cache server base url to the
        repository url.
        """
        p_url = urllib.parse.urlparse(url)
        p_base_url = urllib.parse.urlparse(self.base_url)
        p_url = p_url._replace(scheme=p_base_url.scheme,
                               netloc=p_base_url.netloc)
        return p_url.geturl()

    def serialize(self):
        return {"name": self.name,
                "manifest": {"id": self.manifest.id,
                             "name": str(self.manifest)},
                "public_ip_address": self.public_ip_address,
                "base_url": self.base_url}


# Printers


def ppd_path(instance, filename):
    # TODO overflow ? cleanup ?
    return 'monolith/PPDs/{0:08d}_{1}'.format(instance.pk, filename)


class PrinterPPDManager(models.Manager):
    def get_with_token(self, token):
        from zentral.utils.api_views import API_SECRET
        try:
            pk = int(signing.loads(token, salt="monolith", key=API_SECRET)["pk"])
        except (AttributeError, KeyError, signing.BadSignature):
            logger.error("Bad ppd download URL signature")
            raise ValueError
        else:
            return self.get(pk=pk)


class PrinterPPD(models.Model):
    model_name = models.CharField(max_length=256, editable=False)
    short_nick_name = models.CharField(max_length=256, editable=False)
    manufacturer = models.CharField(max_length=256, editable=False)
    product = ArrayField(models.CharField(max_length=256), editable=False)
    file_version = models.CharField(max_length=256, editable=False)
    pc_file_name = models.CharField(max_length=256, editable=False)  # max_length=12 if stick to std
    file = models.FileField(upload_to=ppd_path, blank=True)
    file_compressed = models.BooleanField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PrinterPPDManager()

    def __str__(self):
        return self.model_name or self.short_nick_name

    def get_absolute_url(self):
        return reverse("monolith:ppd", args=(self.pk,))

    def get_download_url(self):
        from zentral.utils.api_views import API_SECRET
        token = signing.dumps({"pk": self.pk}, salt="monolith", key=API_SECRET)
        return "{}{}".format(settings["api"]["tls_hostname"],
                             reverse("monolith:download_printer_ppd", args=(token,)))

    def get_destination(self):
        if self.file_compressed:
            extension = ".gz"
        else:
            extension = ""
        destination = "{}_v{}_{}{}".format(self.pk, self.file_version, self.pc_file_name, extension)
        return os.path.join("/Library/Printers/PPDs/Contents/Resources", destination)


class Printer(models.Model):
    # TODO VALIDATORS

    ERROR_POLICY_ABORT_JOB = "abort-job"
    ERROR_POLICY_RETRY_JOB = "retry-job"
    ERROR_POLICY_RETRY_CURRENT_JOB = "retry-current-job"
    ERROR_POLICY_STOP_PRINTER = "stop-printer"
    ERROR_POLICY_CHOICES = [(p, p.replace("-", " ").capitalize())
                            for p in (ERROR_POLICY_ABORT_JOB,
                                      ERROR_POLICY_RETRY_JOB,
                                      ERROR_POLICY_RETRY_CURRENT_JOB,
                                      ERROR_POLICY_STOP_PRINTER)]

    SCHEME_IPP = "ipp"
    SCHEME_IPPS = "ipps"
    SCHEME_HTTP = "http"
    SCHEME_HTTPS = "https"
    SCHEME_LPD = "lpd"
    SCHEME_SOCKET = "socket"
    SCHEME_CHOICES = [(s, s) for s in (SCHEME_IPP, SCHEME_IPPS,
                                       SCHEME_HTTP, SCHEME_HTTPS,
                                       SCHEME_LPD, SCHEME_SOCKET)]

    manifest = models.ForeignKey(Manifest, on_delete=models.CASCADE)
    tags = models.ManyToManyField(Tag, blank=True)
    name = models.CharField(max_length=128, help_text="display name of the printer")
    location = models.CharField(max_length=256, blank=True, help_text="location of the printer")
    scheme = models.CharField(max_length=5, choices=SCHEME_CHOICES, default=SCHEME_IPP)
    address = models.CharField(max_length=256)
    shared = models.BooleanField(default=False)
    error_policy = models.CharField(max_length=32, choices=ERROR_POLICY_CHOICES, default=ERROR_POLICY_ABORT_JOB)
    ppd = models.ForeignKey(PrinterPPD, on_delete=models.PROTECT)
    version = models.PositiveSmallIntegerField(default=1)
    required_package = models.ForeignKey(PkgInfoName, on_delete=models.PROTECT, blank=True, null=True)
    pkg_info = JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    trashed_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ('name', 'id')

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.id:
            self.version = 1
        else:
            self.version = F("version") + 1
        super().save(*args, **kwargs)
        self.refresh_from_db()
        self.pkg_info = make_printer_package_info(self)
        super().save(*args, **kwargs)

    def mark_as_trashed(self):
        self.trashed_at = datetime.now()
        super().save()

    def get_pkg_info_name(self):
        """for the manifest"""
        return "manifest {} printer {}".format(self.manifest.id, self.id)

    def get_destination(self):
        """lpadmin destination. name used as display name => info."""
        return self.get_pkg_info_name().replace(" ", "_")


# Enrollment


class Enrollment(BaseEnrollment):
    manifest = models.ForeignKey(Manifest, on_delete=models.CASCADE)

    def get_description_for_distributor(self):
        return "Monolith manifest {}".format(self.manifest)

    def serialize_for_event(self):
        enrollment_dict = super().serialize_for_event()
        enrollment_dict.update({"manifest": {"pk": self.manifest.pk,
                                             "name": str(self.manifest)}})
        return enrollment_dict

    def get_absolute_url(self):
        return "{}#enrollment_{}".format(reverse("monolith:manifest", args=(self.manifest.pk,)), self.pk)


class EnrolledMachine(models.Model):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE)
    serial_number = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("enrollment", "serial_number")
