/*
 * Dilmaç: bridge to libchiaki (chiaki-ng), API in bridge.h.
 *
 * Wraps chiaki-ng's PS5 Remote Play connection library (libchiaki) in a few plain C functions callable from
 * Python ctypes (Windows) and Swift (iOS): registration (pairing with a PIN), wake-up, and the video/audio stream.
 * Video is not decoded here; H.264/H.265 access units are handed over as they are (PyAV / VideoToolbox decodes
 * them). Game audio is decoded from Opus with chiaki's own decoder and handed over as PCM. The controller is not
 * used.
 *
 * libchiaki is licensed under AGPL-3.0; this bridge links it and is under the same license.
 * Build: build.sh (MSYS2 UCRT64) on Windows, ios/scripts/build-chiaki.sh on macOS for iOS.
 */
#include "bridge.h"

#include <chiaki/common.h>
#include <chiaki/discovery.h>
#include <chiaki/log.h>
#include <chiaki/opusdecoder.h>
#include <chiaki/regist.h>
#include <chiaki/session.h>

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
typedef volatile LONG kp_state;
static void state_set(kp_state *p, int v) { InterlockedExchange(p, (LONG)v); }
static int state_get(kp_state *p) { return (int)InterlockedCompareExchange(p, 0, 0); }
static void sleep_ms(int ms) { Sleep((DWORD)ms); }
#define kp_strdup _strdup
#else
#include <stdatomic.h>
#include <time.h>
typedef atomic_int kp_state;
static void state_set(kp_state *p, int v) { atomic_store(p, v); }
static int state_get(kp_state *p) { return atomic_load(p); }
static void sleep_ms(int ms)
{
	struct timespec t = {ms / 1000, (long)(ms % 1000) * 1000000L};
	nanosleep(&t, NULL);
}
#define kp_strdup strdup
#endif

static kp_log_cb g_log_cb = NULL;
static ChiakiLog g_log;
static int g_ready = 0;

static void forward_log(ChiakiLogLevel level, const char *message, void *user)
{
	(void)user;
	if(g_log_cb)
		g_log_cb((int)level, message);
}

KP_EXPORT int kp_version(void) { return 3; }  /* 3: English names; 2: kp_session_open takes an audio callback */

KP_EXPORT const char *kp_error_text(int code) { return chiaki_error_string((ChiakiErrorCode)code); }

/* Call once. level_mask: CHIAKI_LOG_* bits (error=1, warning=2, info=4, verbose=8, debug=16). */
KP_EXPORT int kp_init(kp_log_cb cb, unsigned int level_mask)
{
	g_log_cb = cb;
	chiaki_log_init(&g_log, level_mask, forward_log, NULL);
	if(!g_ready)
	{
		ChiakiErrorCode e = chiaki_lib_init();
		if(e != CHIAKI_ERR_SUCCESS)
			return (int)e;
		g_ready = 1;
	}
	return 0;
}

/* ---------------------------------------------------------------- registration (pairing) */

typedef struct
{
	kp_state state; /* 0 running, 1 success, 2 failed, 3 canceled */
	ChiakiRegisteredHost host;
} RegistResult;

static void on_regist_event(ChiakiRegistEvent *event, void *user)
{
	RegistResult *r = (RegistResult *)user;
	int s = 2;
	if(event->type == CHIAKI_REGIST_EVENT_TYPE_FINISHED_SUCCESS && event->registered_host)
	{
		r->host = *event->registered_host;
		s = 1;
	}
	else if(event->type == CHIAKI_REGIST_EVENT_TYPE_FINISHED_CANCELED)
		s = 3;
	state_set(&r->state, s);
}

/*
 * Registers this PC with the PS5 (the 8-digit code under Settings > System > Remote Play > Link Device).
 * account_id: the PSN account's 8-byte id. On success regist_key[16], rp_key[16], mac[6] and name are filled.
 * Returns: 0 success, 1 rejected by the PS5 (wrong or expired code), 2 timeout, 1000+ could not start
 * (chiaki error code).
 */
KP_EXPORT int kp_register(const char *host, const uint8_t *account_id, uint32_t pin, int ps5, int timeout_ms,
		char *regist_key, uint8_t *rp_key, uint8_t *mac, char *name, size_t name_size)
{
	ChiakiRegistInfo info;
	memset(&info, 0, sizeof(info));
	info.target = ps5 ? CHIAKI_TARGET_PS5_1 : CHIAKI_TARGET_PS4_10;
	info.host = host;
	info.broadcast = false;
	info.psn_online_id = NULL;
	memcpy(info.psn_account_id, account_id, CHIAKI_PSN_ACCOUNT_ID_SIZE);
	info.pin = pin;
	info.console_pin = 0;
	info.holepunch_info = NULL;
	info.rudp = NULL;

	RegistResult r;
	memset(&r, 0, sizeof(r));
	ChiakiRegist regist;
	ChiakiErrorCode e = chiaki_regist_start(&regist, &g_log, &info, on_regist_event, &r);
	if(e != CHIAKI_ERR_SUCCESS)
		return 1000 + (int)e;

	int elapsed = 0;
	while(state_get(&r.state) == 0 && elapsed < timeout_ms)
	{
		sleep_ms(50);
		elapsed += 50;
	}
	int timed_out = state_get(&r.state) == 0;
	if(timed_out)
	{
		chiaki_regist_stop(&regist);
		for(int i = 0; i < 100 && state_get(&r.state) == 0; i++)
			sleep_ms(50);
	}
	chiaki_regist_fini(&regist);
	if(timed_out)
		return 2;
	if(state_get(&r.state) != 1)
		return 1;

	memcpy(regist_key, r.host.rp_regist_key, CHIAKI_SESSION_AUTH_SIZE);
	memcpy(rp_key, r.host.rp_key, 0x10);
	memcpy(mac, r.host.server_mac, 6);
	if(name && name_size)
	{
		strncpy(name, r.host.server_nickname, name_size - 1);
		name[name_size - 1] = '\0';
	}
	return 0;
}

/* ---------------------------------------------------------------- wake-up */

/* Wakes a PS5 from rest mode. regist_key: the 16 bytes from registration (hex text, NUL padded). */
KP_EXPORT int kp_wakeup(const char *host, const char *regist_key, int ps5)
{
	char k[CHIAKI_SESSION_AUTH_SIZE + 1];
	memcpy(k, regist_key, CHIAKI_SESSION_AUTH_SIZE);
	k[CHIAKI_SESSION_AUTH_SIZE] = '\0';
	char *end = NULL;
	uint64_t credential = strtoull(k, &end, 16);
	if(end == k || strlen(k) > 8)
		return 1000;
	return (int)chiaki_discovery_wakeup(&g_log, NULL, host, credential, ps5 != 0);
}

/* ---------------------------------------------------------------- session (video and audio stream) */

typedef struct
{
	ChiakiSession session;
	kp_frame_cb frame;
	kp_event_cb event;
	kp_audio_cb audio;
	void *user;
	char *host;
	ChiakiOpusDecoder opus;
	bool has_opus;
	uint32_t channels, rate;
} Session;

static void on_audio_settings(uint32_t channels, uint32_t rate, void *user)
{
	Session *s = (Session *)user;
	s->channels = channels;
	s->rate = rate;
}

static void on_audio(int16_t *buf, size_t samples, void *user)
{
	Session *s = (Session *)user;
	if(s->audio && s->channels && s->rate)
		s->audio(buf, samples, s->channels, s->rate, s->user);
}

static void session_free(Session *s)
{
	if(s->has_opus)
		chiaki_opus_decoder_fini(&s->opus);
	free(s->host);
	free(s);
}

static bool on_video(uint8_t *buf, size_t size, int32_t frames_lost, bool frame_recovered, void *user)
{
	(void)frame_recovered;
	Session *s = (Session *)user;
	if(!s->frame)
		return true;
	return s->frame(buf, size, frames_lost, s->user) != 0;
}

static void on_event(ChiakiEvent *event, void *user)
{
	Session *s = (Session *)user;
	if(!s->event)
		return;
	switch(event->type)
	{
		case CHIAKI_EVENT_CONNECTED:
			s->event(1, 0, "", s->user);
			break;
		case CHIAKI_EVENT_QUIT:
			s->event(2, (int)event->quit.reason, event->quit.reason_str ? event->quit.reason_str : "", s->user);
			break;
		case CHIAKI_EVENT_LOGIN_PIN_REQUEST:
			s->event(3, event->login_pin_request.pin_incorrect ? 1 : 0, "", s->user);
			break;
		default:
			break;
	}
}

/*
 * Connects to the PS5 and starts the stream. resolution: 1=360p 2=540p 3=720p 4=1080p; fps: 30/60;
 * codec: 0=H.264 1=H.265. If audio is NULL no audio is requested; otherwise game audio is decoded from Opus and
 * delivered as PCM (int16, interleaved). Returns NULL on failure with *error set to the chiaki error code.
 * Frames, audio and events arrive on chiaki's own threads.
 */
KP_EXPORT void *kp_session_open(const char *host, const char *regist_key, const uint8_t *rp_key, int ps5,
		int resolution, int fps, int codec, kp_frame_cb frame, kp_event_cb event, kp_audio_cb audio, void *user,
		int *error)
{
	Session *s = (Session *)calloc(1, sizeof(Session));
	if(!s)
	{
		*error = (int)CHIAKI_ERR_MEMORY;
		return NULL;
	}
	s->host = kp_strdup(host);

	ChiakiConnectInfo info;
	memset(&info, 0, sizeof(info));
	info.ps5 = ps5 != 0;
	info.host = s->host;
	memcpy(info.regist_key, regist_key, sizeof(info.regist_key));
	memcpy(info.morning, rp_key, sizeof(info.morning));
	chiaki_connect_video_profile_preset(&info.video_profile, (ChiakiVideoResolutionPreset)resolution,
			(ChiakiVideoFPSPreset)fps);
	info.video_profile.codec = (ChiakiCodec)codec;
	info.video_profile_auto_downgrade = true;
	info.enable_keyboard = false;
	info.enable_dualsense = false;
	info.audio_video_disabled = audio ? CHIAKI_NONE_DISABLED : CHIAKI_AUDIO_DISABLED;
	info.auto_regist = false;
	info.holepunch_session = NULL;
	info.rudp_sock = NULL;
	info.packet_loss_max = 0.05;
	info.enable_idr_on_fec_failure = true;

	ChiakiErrorCode e = chiaki_session_init(&s->session, &info, &g_log);
	if(e != CHIAKI_ERR_SUCCESS)
	{
		session_free(s);
		*error = (int)e;
		return NULL;
	}
	s->frame = frame;
	s->event = event;
	s->audio = audio;
	s->user = user;
	chiaki_session_set_event_cb(&s->session, on_event, s);
	chiaki_session_set_video_sample_cb(&s->session, on_video, s);
	if(audio)
	{
		ChiakiAudioSink sink;
		chiaki_opus_decoder_init(&s->opus, &g_log);
		s->has_opus = true;
		chiaki_opus_decoder_set_cb(&s->opus, on_audio_settings, on_audio, s);
		chiaki_opus_decoder_get_sink(&s->opus, &sink);
		chiaki_session_set_audio_sink(&s->session, &sink);
	}
	e = chiaki_session_start(&s->session);
	if(e != CHIAKI_ERR_SUCCESS)
	{
		chiaki_session_fini(&s->session);
		session_free(s);
		*error = (int)e;
		return NULL;
	}
	*error = 0;
	return s;
}

/* Stops the stream and frees everything (waits for chiaki's threads to finish). */
KP_EXPORT void kp_session_close(void *p)
{
	Session *s = (Session *)p;
	if(!s)
		return;
	chiaki_session_stop(&s->session);
	chiaki_session_join(&s->session);
	chiaki_session_fini(&s->session);
	session_free(s);
}

/* Asks the PS5 for a new keyframe (IDR), e.g. when the decoder restarts. */
KP_EXPORT int kp_request_keyframe(void *p)
{
	Session *s = (Session *)p;
	return s ? (int)chiaki_session_request_idr(&s->session) : 1000;
}
