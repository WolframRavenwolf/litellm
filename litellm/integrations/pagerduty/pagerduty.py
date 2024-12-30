import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from litellm._logging import verbose_logger
from litellm.integrations.SlackAlerting.slack_alerting import SlackAlerting
from litellm.llms.custom_httpx.http_handler import (
    AsyncHTTPHandler,
    get_async_httpx_client,
    httpxSpecialProvider,
)
from litellm.proxy._types import UserAPIKeyAuth
from litellm.types.integrations.pagerduty import (
    AlertingConfig,
    FailureEvent,
    PagerDutyPayload,
    PagerDutyRequestBody,
)
from litellm.types.utils import (
    StandardLoggingPayload,
    StandardLoggingPayloadErrorInformation,
)


class PagerDutyAlerting(SlackAlerting):
    def __init__(self, alerting_config: AlertingConfig, **kwargs):
        _api_key = os.getenv("PAGERDUTY_API_KEY")
        if not _api_key:
            raise ValueError("PAGERDUTY_API_KEY is not set")
        self.api_key: str = _api_key
        self.alerting_config: AlertingConfig = alerting_config

        # We'll store all recent failures (including error info) in a single list.
        self._failure_events: List[FailureEvent] = []

        super().__init__(**kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """
        Record a failure event. Only send an alert to PagerDuty if the
        configured failure threshold is exceeded in the specified window.
        """
        now = datetime.now(timezone.utc)

        # Extract info from the standard logging object
        standard_logging_payload: Optional[StandardLoggingPayload] = kwargs.get(
            "standard_logging_object"
        )
        if standard_logging_payload is None:
            raise ValueError(
                "standard_logging_object is required for PagerDutyAlerting"
            )
        _event_metadata = standard_logging_payload.get("metadata") or {}
        error_info: Optional[StandardLoggingPayloadErrorInformation] = (
            standard_logging_payload.get("error_information") or {}
        )

        # Create a FailureEvent and add it to our list
        self._failure_events.append(
            FailureEvent(
                timestamp=now,
                error_class=error_info.get("error_class"),
                error_code=error_info.get("error_code"),
                error_llm_provider=error_info.get("llm_provider"),
                user_api_key_hash=_event_metadata.get("user_api_key_hash"),
                user_api_key_alias=_event_metadata.get("user_api_key_alias"),
                user_api_key_org_id=_event_metadata.get("user_api_key_org_id"),
                user_api_key_team_id=_event_metadata.get("user_api_key_team_id"),
                user_api_key_user_id=_event_metadata.get("user_api_key_user_id"),
                user_api_key_team_alias=_event_metadata.get("user_api_key_team_alias"),
                user_api_key_end_user_id=_event_metadata.get(
                    "user_api_key_end_user_id"
                ),
            )
        )

        # Prune events older than our threshold window
        window_seconds = self.alerting_config.get(
            "failure_threshold_window_seconds", 60
        )
        threshold = self.alerting_config.get("failure_threshold", 1)
        cutoff = now - timedelta(seconds=window_seconds)
        self._prune_failure_events(cutoff)

        # If the number of recent failures crosses the threshold, alert PagerDuty
        if len(self._failure_events) >= threshold:
            error_summaries = self._build_error_summaries(max_errors=5)
            alert_message = (
                f"High LLM API Failure Rate: {len(self._failure_events)} failures "
                f"in the last {window_seconds} seconds."
            )

            # Instead of just a text summary, we can also provide more details
            # in the "custom_details" field of the PagerDuty payload
            custom_details = {"recent_errors": error_summaries}

            await self.send_alert_to_pagerduty(
                alert_message=alert_message,
                custom_details=custom_details,
            )

            # Clear the list of failure events after sending the alert
            self._failure_events = []

    async def pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        data: Optional[dict],
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
            "pass_through_endpoint",
            "rerank",
        ],
    ) -> Optional[dict]:
        """
        Pre-Call hook - sleeps for 'self.alerting_threshold' seconds and checks if response has completed

        If not then counts as a hanging request and sends an alert to PagerDuty
        """
        verbose_logger.info("Inside Proxy Logging Pre-call hook!")
        ### ALERTING ###
        asyncio.create_task(self.response_taking_too_long(request_data=data))

    async def send_alert_to_pagerduty(self, alert_message: str, custom_details: dict):
        """
        Send [High] Alert to PagerDuty

        https://developer.pagerduty.com/api-reference/YXBpOjI3NDgyNjU-pager-duty-v2-events-api
        """
        async_client: AsyncHTTPHandler = get_async_httpx_client(
            llm_provider=httpxSpecialProvider.LoggingCallback
        )

        # Insert the 'custom_details' into the PagerDuty payload
        payload: PagerDutyRequestBody = PagerDutyRequestBody(
            payload=PagerDutyPayload(
                summary=alert_message,
                severity="critical",
                source="LiteLLM Alert",
                component="LiteLLM",
                custom_details=custom_details,
            ),
            routing_key=self.api_key,
            event_action="trigger",
        )

        return await async_client.post(
            url="https://events.pagerduty.com/v2/enqueue",
            json=dict(payload),
            headers={"Content-Type": "application/json"},
        )

    # ------------------ Helpers ------------------ #

    def _prune_failure_events(self, cutoff: datetime):
        """Remove any events that are older than the cutoff time."""
        _failure_events = []
        for fe in self._failure_events:
            _timestamp = fe.get("timestamp")
            if _timestamp and _timestamp > cutoff:
                _failure_events.append(fe)
        self._failure_events = _failure_events

    def _build_error_summaries(self, max_errors: int = 5) -> List[str]:
        """
        Build short text summaries for the last `max_errors` events.
        Example: "ValueError (code: 500, provider: openai)"
        """
        # Take only the last few errors
        recent_events = self._failure_events[-max_errors:]
        summaries = []
        for fe in recent_events:
            # If any of these is None, show "N/A" to avoid messing up the summary string
            error_class = fe.get("error_class") or "N/A"
            error_code = fe.get("error_code") or "N/A"
            error_llm_provider = fe.get("error_llm_provider") or "N/A"

            summaries.append(
                f"{error_class} (code: {error_code}, provider: {error_llm_provider})"
            )
        return summaries