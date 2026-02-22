# n8n Scheduled Upload Workflow (Instagram only)

When scheduling uploads:
- YouTube uses native `publishAt`.
- Facebook uses native Reel scheduling (`video_state=SCHEDULED`) directly in backend.
- Instagram is deferred to n8n because Reel scheduling is not natively supported on the IG Graph API.

## Webhook payload

The backend sends one JSON payload to `ATR_N8N_WEBHOOK_URL` for Instagram only.

```json
{
  "project_id": "2ee46c92a4ce",
  "scheduled_at": "2026-02-22T06:10:00+00:00",
  "drive_video_id": "19H1QQG...",
  "graph_api_version": "v25.0",
  "instagram": {
    "ig_user_id": "17841449009893506",
    "ig_access_token": "(IG token)",
    "caption": "Instagram caption..."
  },
  "discord_webhook_url": "https://discord.com/api/webhooks/..."
}
```

## Expected n8n behavior

1. Wait until `scheduled_at`.
2. Download the source video from Drive using `drive_video_id`.
3. Create IG Reel container (`media_type=REELS`).
4. Upload the video.
5. Poll processing status until ready.
6. Publish via `/{ig_user_id}/media_publish`.
7. Notify Discord (success/failure) if webhook URL is provided.

## Backend behavior summary

- Facebook scheduled upload is native-only (no n8n monitor, no fallback delete/republish flow).
- If native Facebook scheduling fails, it is treated as a platform error and surfaced in upload result/Discord notification.
- n8n is used only for deferred Instagram publishing.

## Environment

Set:

```bash
ATR_N8N_WEBHOOK_URL=https://your-n8n-instance.com/webhook/instagram-scheduled-upload
ATR_META_GRAPH_API_VERSION=v25.0
```

If `ATR_N8N_WEBHOOK_URL` is empty, Instagram upload is skipped by backend.
