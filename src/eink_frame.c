/* 선택적으로 최종 게임 화면을 최신 PPM 한 장으로 내보낸다. GPL-3.0-or-later. */
#include "eink_frame.h"

#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EINK_ROW_BYTES (320 * 3)
#define EINK_FRAME_BYTES (EINK_ROW_BYTES * 200)

static struct {
	SDL_mutex *mutex;
	SDL_cond *ready;
	SDL_Thread *thread;
	uint8_t *pending_pixels, *writing_pixels;
	char *path, *temporary_path;
	uint64_t sequence, pending_sequence;
	SDL_bool initialized, pending, stopping;
	const char *failure;
} eink;

static int write_frame(const uint8_t *pixels, uint64_t sequence) {
	FILE *file = fopen(eink.temporary_path, "wb");
	if (file == NULL) return -1;
	int ok = fprintf(file, "P6\n# eink sequence %" PRIu64 "\n320 200\n255\n", sequence) > 0;
	if (ok) ok = fwrite(pixels, 1, EINK_FRAME_BYTES, file) == EINK_FRAME_BYTES;
	int saved_errno = ok ? 0 : errno;
	if (fclose(file) != 0) {
		ok = 0;
		saved_errno = errno;
	}
	if (ok && rename(eink.temporary_path, eink.path) == 0) return 0;
	if (ok) saved_errno = errno;
	remove(eink.temporary_path);
	errno = saved_errno;
	return -1;
}

static int frame_writer(void *unused) {
	(void)unused;
	for (;;) {
		SDL_LockMutex(eink.mutex);
		while (!eink.pending && !eink.stopping && !eink.failure) {
			if (SDL_CondWait(eink.ready, eink.mutex) != 0) {
				eink.failure = "작업 스레드 대기 실패";
			}
		}
		if (eink.failure || (eink.stopping && !eink.pending)) {
			const char *failure = eink.failure;
			SDL_UnlockMutex(eink.mutex);
			if (failure) fprintf(stderr, "eink 화면 추출 중단: %s\n", failure);
			return 0;
		}
		// 기록 중인 버퍼는 메인 스레드가 건드리지 않으며 대기 슬롯만 최신으로 바뀐다.
		uint8_t *spare = eink.writing_pixels;
		eink.writing_pixels = eink.pending_pixels;
		eink.pending_pixels = spare;
		uint64_t sequence = eink.pending_sequence;
		eink.pending = SDL_FALSE;
		SDL_UnlockMutex(eink.mutex);

		if (write_frame(eink.writing_pixels, sequence) != 0) {
			SDL_LockMutex(eink.mutex);
			eink.failure = "PPM 파일 기록 또는 원자 교체 실패";
			SDL_UnlockMutex(eink.mutex);
		}
	}
}

static void release_resources(void) {
	if (eink.ready) SDL_DestroyCond(eink.ready);
	if (eink.mutex) SDL_DestroyMutex(eink.mutex);
	free(eink.pending_pixels);
	free(eink.writing_pixels);
	free(eink.path);
	free(eink.temporary_path);
	eink.ready = NULL;
	eink.mutex = NULL;
	eink.pending_pixels = eink.writing_pixels = NULL;
	eink.path = eink.temporary_path = NULL;
}

void eink_frame_init(void) {
	if (eink.initialized) return;
	eink.initialized = SDL_TRUE;
	const char *path = getenv("SDLPOP_EINK_FRAME_PATH");
	if (path == NULL || path[0] == '\0') return;

	size_t path_length = strlen(path);
	eink.path = malloc(path_length + 1);
	eink.temporary_path = malloc(path_length + 5);
	eink.pending_pixels = malloc(EINK_FRAME_BYTES);
	eink.writing_pixels = malloc(EINK_FRAME_BYTES);
	eink.mutex = SDL_CreateMutex();
	eink.ready = SDL_CreateCond();
	if (!eink.path || !eink.temporary_path || !eink.pending_pixels || !eink.writing_pixels ||
		!eink.mutex || !eink.ready) {
		fprintf(stderr, "eink 화면 추출 비활성: 메모리 또는 동기화 객체 생성 실패\n");
		release_resources();
		return;
	}
	memcpy(eink.path, path, path_length + 1);
	memcpy(eink.temporary_path, path, path_length);
	memcpy(eink.temporary_path + path_length, ".tmp", 5);
	eink.thread = SDL_CreateThread(frame_writer, "eink-frame", NULL);
	if (!eink.thread) {
		fprintf(stderr, "eink 화면 추출 비활성: 작업 스레드 생성 실패\n");
		release_resources();
	}
}

void eink_frame_offer(SDL_Surface *surface) {
	if (!eink.thread || SDL_TryLockMutex(eink.mutex) != 0) return;
	if (eink.stopping || eink.failure) {
		SDL_UnlockMutex(eink.mutex);
		return;
	}
	// 최종 합성 화면만 받으며, 렌더 스레드에서 변환·파일 I/O·대기를 하지 않는다.
	if (!surface || !surface->pixels || !surface->format || surface->w != 320 ||
		surface->h != 200 || surface->format->format != SDL_PIXELFORMAT_RGB24 ||
		surface->format->BytesPerPixel != 3 || surface->pitch < EINK_ROW_BYTES ||
		SDL_MUSTLOCK(surface)) {
		eink.failure = "320×200 비압축 RGB24 화면 또는 행 간격 조건 불일치";
	} else {
		for (int row = 0; row < 200; ++row) {
			memcpy(eink.pending_pixels + row * EINK_ROW_BYTES,
				(const uint8_t *)surface->pixels + (size_t)row * surface->pitch, EINK_ROW_BYTES);
		}
		eink.pending_sequence = ++eink.sequence;
		eink.pending = SDL_TRUE;
	}
	SDL_CondSignal(eink.ready);
	SDL_UnlockMutex(eink.mutex);
}

void eink_frame_stop(void) {
	if (!eink.thread) return;
	SDL_LockMutex(eink.mutex);
	eink.stopping = SDL_TRUE;
	SDL_CondSignal(eink.ready);
	SDL_UnlockMutex(eink.mutex);
	// 정상 종료에서만 기다리며 마지막 대기 화면도 기록한 뒤 자원을 해제한다.
	SDL_WaitThread(eink.thread, NULL);
	eink.thread = NULL;
	release_resources();
}
