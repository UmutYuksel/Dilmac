/*
 * Dilmaç: plain C API over libchiaki (chiaki-ng). Implemented in bridge.c.
 * Used from Python (ctypes, Windows DLL) and from Swift (iOS static library / xcframework).
 *
 * libchiaki is licensed under AGPL-3.0; this bridge links it and is under the same license.
 */
#ifndef DILMAC_BRIDGE_H
#define DILMAC_BRIDGE_H

#include <stddef.h>
#include <stdint.h>

#ifdef _WIN32
#define KP_EXPORT __declspec(dllexport)
#else
#define KP_EXPORT __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*kp_log_cb)(int level, const char *message);
/* One video access unit. Returning 0 marks it as corrupt; a new keyframe is requested from the PS5. */
typedef int (*kp_frame_cb)(const uint8_t *buf, size_t size, int32_t frames_lost, void *user);
/* kind: 1 connected, 2 ended (code = ChiakiQuitReason, text = reason), 3 PS5 asks for a login PIN
 * (code = 1 if the previous one was wrong) */
typedef void (*kp_event_cb)(int kind, int code, const char *text, void *user);
/* Game audio: samples = per channel; pcm holds samples*channels int16 values (interleaved); rate in Hz. */
typedef void (*kp_audio_cb)(const int16_t *pcm, size_t samples, uint32_t channels, uint32_t rate, void *user);

KP_EXPORT int kp_version(void);
KP_EXPORT const char *kp_error_text(int code);
/* Call once. level_mask: CHIAKI_LOG_* bits (error=1, warning=2, info=4, verbose=8, debug=16). */
KP_EXPORT int kp_init(kp_log_cb cb, unsigned int level_mask);

/*
 * Registers this device with the PS5 (the 8-digit code under Settings > System > Remote Play > Link Device).
 * account_id: the PSN account's 8-byte id (little endian). On success regist_key[16], rp_key[16], mac[6] and
 * name are filled. Blocks for up to timeout_ms.
 * Returns: 0 success, 1 rejected by the PS5 (wrong or expired code), 2 timeout, 1000+ could not start
 * (chiaki error code).
 */
KP_EXPORT int kp_register(const char *host, const uint8_t *account_id, uint32_t pin, int ps5, int timeout_ms,
		char *regist_key, uint8_t *rp_key, uint8_t *mac, char *name, size_t name_size);

/* Wakes a PS5 from rest mode. regist_key: the 16 bytes from registration (hex text, NUL padded). */
KP_EXPORT int kp_wakeup(const char *host, const char *regist_key, int ps5);

/*
 * Connects to the PS5 and starts the stream. resolution: 1=360p 2=540p 3=720p 4=1080p; fps: 30/60;
 * codec: 0=H.264 1=H.265. If audio is NULL no audio is requested. Returns NULL on failure with *error set to the
 * chiaki error code. Frames, audio and events arrive on chiaki's own threads.
 */
KP_EXPORT void *kp_session_open(const char *host, const char *regist_key, const uint8_t *rp_key, int ps5,
		int resolution, int fps, int codec, kp_frame_cb frame, kp_event_cb event, kp_audio_cb audio, void *user,
		int *error);

/* Stops the stream and frees everything (waits for chiaki's threads to finish). */
KP_EXPORT void kp_session_close(void *session);

/* Asks the PS5 for a new keyframe (IDR), e.g. when the decoder restarts. */
KP_EXPORT int kp_request_keyframe(void *session);

#ifdef __cplusplus
}
#endif

#endif /* DILMAC_BRIDGE_H */
