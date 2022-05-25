import json
import logging
from django.http import HttpResponse, JsonResponse
from django.views.generic import View
from .events import post_okta_events
from .models import EventHook


logger = logging.getLogger('zentral.contrib.okta.views')


class EventHookView(View):
    def get(self, request, *args, **kwargs):
        return JsonResponse({"verification": request.headers.get("X-Okta-Verification-Challenge")})

    def post(self, request, *args, **kwargs):
        try:
            authorization_key = request.headers.get("Authorization")
            event_hook = EventHook.objects.get(authorization_key=authorization_key)
            data = json.loads(request.body.decode("utf-8"))
            post_okta_events(event_hook, data)
        except Exception:
            logger.exception("Could not process Okta event hook request")
        return HttpResponse("")
