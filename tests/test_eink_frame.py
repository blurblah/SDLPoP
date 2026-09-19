#!/usr/bin/env python3
"""실제 exporter와 SDL 스레드로 검증한다. 게임 창·장치·USB를 사용하지 않는다."""

from pathlib import Path
import shlex
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build/eink-frame-test"

HARNESS = r'''
#include <SDL2/SDL.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *scenario;
static SDL_threadID main_thread;
static SDL_sem *write_paused, *write_release, *lock_held, *lock_release;
static int io_calls, frame_writes;

static void require(int condition, const char *message) {
    if (!condition) { fprintf(stderr, "FAIL %s: %s\n", scenario, message); exit(1); }
}
static void worker_only(void) {
    require(SDL_ThreadID() != main_thread, "메인 스레드에서 파일 I/O 발생");
    ++io_calls;
}
static void check_pixels(const uint8_t *pixels, unsigned seed) {
    for (unsigned row = 0; row < 200; ++row)
        for (unsigned column = 0; column < 960; ++column)
            require(pixels[row * 960 + column] == (uint8_t)(row + column + seed),
                    "RGB 픽셀 또는 행 간격 불일치");
}
static FILE *checked_fopen(const char *path, const char *mode) {
    worker_only();
    if (!strcmp(scenario, "open_failure")) { errno = EACCES; return NULL; }
    return fopen(path, mode);
}
static size_t checked_fwrite(const void *pixels, size_t size, size_t count, FILE *file) {
    worker_only();
    require(size == 1 && count == 192000, "PPM payload 크기");
    ++frame_writes;
    if (!strcmp(scenario, "short_write")) {
        errno = ENOSPC;
        return fwrite(pixels, size, count - 1, file);
    }
    if (!strcmp(scenario, "latest") && frame_writes == 1) {
        check_pixels(pixels, 1);
        size_t first = fwrite(pixels, 1, count / 2, file);
        require(first == count / 2 && fflush(file) == 0, "임시 파일 반쪽 기록");
        SDL_SemPost(write_paused);
        require(SDL_SemWaitTimeout(write_release, 5000) == 0, "파일 I/O 중 메인 스레드가 막힘");
        check_pixels(pixels, 1);
        return first + fwrite((const uint8_t *)pixels + first, 1, count - first, file);
    }
    return fwrite(pixels, size, count, file);
}
static int checked_fclose(FILE *file) {
    worker_only();
    int result = fclose(file);
    if (!strcmp(scenario, "close_failure")) { errno = EIO; return EOF; }
    return result;
}
static int checked_rename(const char *from, const char *to) {
    worker_only();
    if (!strcmp(scenario, "rename_failure")) { errno = EACCES; return -1; }
    return rename(from, to);
}
static int checked_remove(const char *path) { worker_only(); return remove(path); }

// 복제 구현 대신 생산 모듈을 포함하고 파일 쓰기의 실패·대기만 주입한다.
#define fopen checked_fopen
#define fwrite checked_fwrite
#define fclose checked_fclose
#define rename checked_rename
#define remove checked_remove
#include "../../src/eink_frame.c"
#undef fopen
#undef fwrite
#undef fclose
#undef rename
#undef remove

static int hold_mutex(void *unused) {
    (void)unused;
    SDL_LockMutex(eink.mutex);
    SDL_SemPost(lock_held);
    require(SDL_SemWaitTimeout(lock_release, 3000) == 0, "비차단 잠금 대신 메인 스레드가 대기함");
    SDL_UnlockMutex(eink.mutex);
    return 0;
}
static void fill_surface(SDL_Surface *surface, unsigned seed) {
    for (unsigned row = 0; row < 200; ++row)
        for (unsigned column = 0; column < 960; ++column)
            ((uint8_t *)surface->pixels)[row * surface->pitch + column] = (uint8_t)(row + column + seed);
}
static void offer_first(SDL_Surface *surface) {
    // 시작 순간 worker가 잠금을 가진 경우의 합법적인 프레임 생략은 재시도한다.
    for (int attempt = 0; attempt < 3000; ++attempt) {
        eink_frame_offer(surface);
        SDL_LockMutex(eink.mutex);
        int accepted = eink.sequence != 0 || eink.failure != NULL;
        SDL_UnlockMutex(eink.mutex);
        if (accepted) return;
        SDL_Delay(1);
    }
    require(0, "첫 화면 수락 또는 오류 판정 없음");
}
static void seed_previous(const char *path) {
    FILE *file = fopen(path, "wb");
    require(file != NULL && fputs("previous-complete", file) >= 0 && fclose(file) == 0, "이전 파일 준비");
}
static void check_previous(const char *path) {
    FILE *file = fopen(path, "rb");
    char bytes[32] = {0};
    require(file != NULL, "이전 출력 파일 소실");
    size_t length = fread(bytes, 1, sizeof(bytes), file);
    fclose(file);
    require(length == strlen("previous-complete") && !strcmp(bytes, "previous-complete"),
            "완성 전 또는 오류에서 이전 파일이 바뀜");
}
static void check_final(const char *path, uint64_t sequence, unsigned seed) {
    char header[128];
    int length = snprintf(header, sizeof(header), "P6\n# eink sequence %" PRIu64 "\n320 200\n255\n", sequence);
    FILE *file = fopen(path, "rb");
    require(file != NULL, "완성 파일 없음");
    uint8_t *bytes = malloc(192000 + sizeof(header));
    require(bytes != NULL, "검증 버퍼 할당");
    size_t received = fread(bytes, 1, 192000 + sizeof(header), file);
    fclose(file);
    require(received == (size_t)length + 192000 && !memcmp(bytes, header, length), "정확한 PPM 헤더·파일 크기");
    check_pixels(bytes + length, seed);
    free(bytes);
}
int main(int argc, char **argv) {
    require(argc == 3, "인수: 시나리오 출력경로");
    scenario = argv[1];
    main_thread = SDL_ThreadID();
    require(SDL_Init(SDL_INIT_TIMER) == 0, "SDL 타이머 초기화");
    if (!strcmp(scenario, "disabled")) unsetenv("SDLPOP_EINK_FRAME_PATH");
    else require(setenv("SDLPOP_EINK_FRAME_PATH", !strcmp(scenario, "empty") ? "" : argv[2], 1) == 0,
                 "환경 변수 설정");
    seed_previous(argv[2]);
    eink_frame_init();
    if (!strcmp(scenario, "disabled") || !strcmp(scenario, "empty")) {
        require(!eink.thread && !eink.mutex && !eink.pending_pixels, "opt-out에서 객체 생성");
        eink_frame_offer(NULL);
        eink_frame_stop();
        require(io_calls == 0, "opt-out에서 I/O 발생");
        check_previous(argv[2]);
    } else {
        require(eink.thread != NULL, "exporter 초기화 실패");
        uint8_t pixels[200 * 976];
        memset(pixels, 0xA5, sizeof(pixels));
        SDL_Surface *surface = SDL_CreateRGBSurfaceWithFormatFrom(pixels, 320, 200, 24, 976, SDL_PIXELFORMAT_RGB24);
        require(surface != NULL, "패딩 있는 RGB24 surface 생성");
        fill_surface(surface, 1);
        if (!strcmp(scenario, "latest")) {
            write_paused = SDL_CreateSemaphore(0); write_release = SDL_CreateSemaphore(0);
            lock_held = SDL_CreateSemaphore(0); lock_release = SDL_CreateSemaphore(0);
            require(write_paused && write_release && lock_held && lock_release, "검증 세마포어 생성");
            offer_first(surface);
            require(SDL_SemWaitTimeout(write_paused, 3000) == 0, "작업 스레드가 파일을 기록하지 않음");
            check_previous(argv[2]);
            SDL_Thread *locker = SDL_CreateThread(hold_mutex, "eink-lock-test", NULL);
            require(locker && SDL_SemWaitTimeout(lock_held, 3000) == 0, "검증 잠금 획득");
            eink_frame_offer(surface);
            SDL_SemPost(lock_release);
            SDL_WaitThread(locker, NULL);
            require(eink.sequence == 1, "잠금 중 프레임을 대기하거나 수락함");
            // 파일 기록이 멈춰 있어도 게임의 최신 슬롯 복사는 계속되고 큐는 쌓이지 않는다.
            for (unsigned seed = 2; seed <= 101; ++seed) {
                fill_surface(surface, seed);
                eink_frame_offer(surface);
            }
            require(eink.pending && eink.pending_sequence == 101, "최신 대기 슬롯 교체 실패");
            check_previous(argv[2]);
            SDL_SemPost(write_release);
            eink_frame_stop();
            require(frame_writes == 2, "중간 화면을 누적하거나 마지막 대기 화면 유실");
            check_final(argv[2], 101, 101);
            SDL_DestroySemaphore(write_paused); SDL_DestroySemaphore(write_release);
            SDL_DestroySemaphore(lock_held); SDL_DestroySemaphore(lock_release);
        } else if (!strncmp(scenario, "invalid_", 8)) {
            if (!strcmp(scenario, "invalid_pitch")) surface->pitch = 959;
            else if (!strcmp(scenario, "invalid_width")) surface->w = 319;
            else if (!strcmp(scenario, "invalid_format")) surface->format->format = SDL_PIXELFORMAT_BGR24;
            else if (!strcmp(scenario, "invalid_lock")) surface->flags |= SDL_RLEACCEL;
            offer_first(!strcmp(scenario, "invalid_null") ? NULL : surface);
            // 직접 만든 surface의 해제에 영향이 없도록 검증용 변경은 복원한다.
            surface->pitch = 976; surface->w = 320;
            surface->format->format = SDL_PIXELFORMAT_RGB24; surface->flags &= ~SDL_RLEACCEL;
            eink_frame_stop();
            require(io_calls == 0, "잘못된 surface가 파일로 기록됨");
            check_previous(argv[2]);
        } else {
            offer_first(surface);
            eink_frame_stop();
            if (!strcmp(scenario, "normal")) check_final(argv[2], 1, 1);
            else check_previous(argv[2]);
        }
        SDL_FreeSurface(surface);
        require(!eink.thread && !eink.mutex && !eink.pending_pixels && !eink.writing_pixels, "종료 자원 해제");
        int previous_io = io_calls;
        eink_frame_offer(NULL); eink_frame_stop();
        require(io_calls == previous_io, "종료 후 추가 작업");
    }
    SDL_Quit();
    printf("PASS %s: 실제 exporter·SDL 스레드 검증, GUI/USB 접근 없음\n", scenario);
    return 0;
}
'''


def main():
    compiler = shutil.which("clang") or shutil.which("cc")
    if compiler is None:
        raise SystemExit("C 컴파일러를 찾지 못했습니다")
    BUILD.mkdir(parents=True, exist_ok=True)
    source = BUILD / "harness.c"
    # 생성 위치에서 실제 모듈을 include하도록 절대 경로로 바꾼다.
    include_path = str(ROOT / "src/eink_frame.c").replace("\\", "\\\\").replace('"', '\\"')
    source.write_text(HARNESS.replace('"../../src/eink_frame.c"', f'"{include_path}"'))
    flags = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", "sdl2"], text=True))
    command = [compiler, "-std=c99", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror",
               str(source), "-o", str(BUILD / "harness"), *flags]
    subprocess.run(command, check=True)
    scenarios = ("normal", "latest", "disabled", "empty", "invalid_null", "invalid_pitch",
                 "invalid_width", "invalid_format", "invalid_lock", "open_failure", "short_write",
                 "close_failure", "rename_failure")
    for scenario in scenarios:
        subprocess.run([str(BUILD / "harness"), scenario, str(BUILD / f"{scenario}.ppm")],
                       check=True, timeout=15)
    print(f"PASS 전체 {len(scenarios)}개 시나리오")


if __name__ == "__main__":
    main()
