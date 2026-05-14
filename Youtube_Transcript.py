from youtube_transcript_api import YouTubeTranscriptApi
from urllib.parse import urlparse, parse_qs

def get_video_id(youtube_url):
    """Extract video ID from YouTube URL"""
    query = urlparse(youtube_url)
    if query.hostname == 'youtu.be':
        return query.path[1:]
    elif query.hostname in ('www.youtube.com', 'youtube.com'):
        if query.path == '/watch':
            if 'v' in parse_qs(query.query):
                return parse_qs(query.query)['v'][0]
    return None

def get_transcript_from_url(youtube_url):
    """Get transcript from YouTube URL with English preference and safe fallbacks."""
    video_id = get_video_id(youtube_url)
    if video_id:
        try:
            api = YouTubeTranscriptApi()
            preferred_languages = ['en', 'en-US', 'en-GB']

            def transcript_to_text(items):
                chunks = []
                for item in items:
                    if hasattr(item, 'text'):
                        chunks.append(item.text)
                    elif isinstance(item, dict):
                        chunks.append(item.get('text', ''))
                return ' '.join([chunk for chunk in chunks if chunk]).strip()

            # 1) Try direct English transcript first.
            try:
                fetched = api.fetch(video_id, languages=preferred_languages)
                direct_english = transcript_to_text(fetched)
                if direct_english:
                    return direct_english
            except Exception:
                pass

            # 2) Enumerate available transcript tracks.
            transcript_list = api.list(video_id)

            selected_track = None

            # Prefer English if present in listing.
            for transcript in transcript_list:
                code = (getattr(transcript, 'language_code', '') or '').lower()
                if code.startswith('en'):
                    selected_track = transcript
                    break

            # Else prefer manually created caption track.
            if selected_track is None:
                for transcript in transcript_list:
                    if getattr(transcript, 'is_generated', True) is False:
                        selected_track = transcript
                        break

            # Else use first available track.
            if selected_track is None:
                for transcript in transcript_list:
                    selected_track = transcript
                    break

            if selected_track is None:
                return "Error getting transcript: No transcript tracks are available for this video"

            selected_lang = (getattr(selected_track, 'language_code', '') or '').lower()

            # 3) If selected track is already English, return it.
            if selected_lang.startswith('en'):
                fetched = selected_track.fetch()
                english_text = transcript_to_text(fetched)
                if english_text:
                    return english_text

            # 4) Otherwise try translating selected track to English.
            try:
                if getattr(selected_track, 'is_translatable', False):
                    translated = selected_track.translate('en').fetch()
                    translated_text = transcript_to_text(translated)
                    if translated_text:
                        return translated_text
            except Exception:
                pass

            # 5) Try other tracks for English or translatable to English.
            for transcript in transcript_list:
                try:
                    code = (getattr(transcript, 'language_code', '') or '').lower()
                    if code.startswith('en'):
                        fetched = transcript.fetch()
                        text = transcript_to_text(fetched)
                        if text:
                            return text

                    if getattr(transcript, 'is_translatable', False):
                        translated = transcript.translate('en').fetch()
                        translated_text = transcript_to_text(translated)
                        if translated_text:
                            return translated_text
                except Exception:
                    continue

            # 6) Final fallback: return original-language captions instead of failing.
            try:
                fallback_items = selected_track.fetch()
                fallback_text = transcript_to_text(fallback_items)
                if fallback_text:
                    language_name = getattr(selected_track, 'language', None) or getattr(selected_track, 'language_code', 'unknown')
                    return f"[Transcript language: {language_name}]\n\n{fallback_text}"
            except Exception:
                pass

            return "Error getting transcript: No usable transcript track could be extracted"
        except Exception as e:
            error_text = str(e)
            lower_error = error_text.lower()
            if "timed out" in lower_error or "winerror 10060" in lower_error or "connection" in lower_error:
                return "Error getting transcript: Network timeout while contacting YouTube. Please retry in a moment."
            return f"Error getting transcript: {error_text}"
    else:
        return "Invalid URL"

# Example usage (when run directly)
if __name__ == "__main__":
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    transcript = get_transcript_from_url(url)
    print(transcript)
