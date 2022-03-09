import json
import logging
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.views.generic import TemplateView, View
from zentral.utils.api_views import APIAuthError
from zentral.utils.http import user_agent_and_ip_address_from_request
from .conf import puppet_conf
from .events import post_puppet_report


logger = logging.getLogger('zentral.contrib.puppet.views')


# setup > puppet instances


class InstancesView(LoginRequiredMixin, TemplateView):
    template_name = "puppet/instance_list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["setup"] = True
        ctx["instances"] = list(puppet_conf.get_instances_with_secrets())
        instances_count = len(ctx["instances"])
        if instances_count == 0 or instances_count > 1:
            suffix = "s"
        else:
            suffix = ""
        ctx["title"] = "{} puppet instance{}".format(instances_count, suffix)
        return ctx


# API


class PostReportView(View):
    def post(self, request, *args, **kwargs):
        try:
            instance = puppet_conf.get_instance_with_secret(kwargs["secret"])
        except APIAuthError:
            return HttpResponseForbidden("Forbidden")
        except KeyError:
            return HttpResponseNotFound("Unknown puppet instance")

        try:
            report = json.load(request)
        except Exception:
            raise SuspiciousOperation("Could not parse report")

        # trim the report
        report.pop("logs", None)
        report.pop("metrics", None)
        report.pop("resource_statuses", None)

        user_agent, ip = user_agent_and_ip_address_from_request(request)
        post_puppet_report(instance, user_agent, ip, report)
        return HttpResponse("OK")
