/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (C) 2026 PulsarOS Intelligence Inc.
 *
 * pinclock.so: a wall clock pinned for the demo recordings (tools/demo/make_all.sh), so a replay never
 * carries the recording machine's time of day ("done 1:22 AM") and reruns show the same clock.
 *
 *   cc -shared -fPIC -O2 -o pinclock.so pinclock.c -ldl
 *   LD_PRELOAD=pinclock.so DEMO_PIN_EPOCH=1790847660 DEMO_PIN_EXE=claude <command>
 *   LD_PRELOAD=pinclock.so DEMO_PIN_EPOCH=1790847660 DEMO_PIN_ORIGIN=1790790000 DEMO_PIN_EXE=python3.11,claude ...
 *
 * Only a process whose executable is named in DEMO_PIN_EXE (a comma-separated list of basenames) is
 * affected; any other keeps the real clock. Without DEMO_PIN_ORIGIN, its CLOCK_REALTIME starts at
 * DEMO_PIN_EPOCH when the process starts. With DEMO_PIN_ORIGIN (a real epoch, in seconds), every pinned
 * process shares one clock that reads DEMO_PIN_EPOCH at that real instant, so a Core started before
 * its viewer agrees with it. Either way the clock then runs at the real rate; monotonic clocks are
 * untouched. Dev-only, never shipped to users.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static int active;
static long long offset_ns;                 /* pinned - real, fixed at start */
static int (*real_clock_gettime)(clockid_t, struct timespec *);

static long long now_ns(clockid_t id) {
    struct timespec ts;
    real_clock_gettime(id, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

__attribute__((constructor)) static void pin_init(void) {
    real_clock_gettime = (int (*)(clockid_t, struct timespec *))dlsym(RTLD_NEXT, "clock_gettime");
    const char *epoch = getenv("DEMO_PIN_EPOCH"), *want = getenv("DEMO_PIN_EXE");
    if (!real_clock_gettime || !epoch || !want)
        return;
    char exe[4096];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n <= 0)
        return;
    exe[n] = 0;
    const char *base = strrchr(exe, '/');
    base = base ? base + 1 : exe;
    size_t len = strlen(base);
    const char *p = want;
    int match = 0;
    while (*p) {                            /* DEMO_PIN_EXE: comma-separated basenames */
        const char *end = strchr(p, ',');
        size_t n2 = end ? (size_t)(end - p) : strlen(p);
        if (n2 == len && strncmp(p, base, len) == 0)
            match = 1;
        if (!end)
            break;
        p = end + 1;
    }
    if (!match)
        return;
    const char *origin = getenv("DEMO_PIN_ORIGIN");
    if (origin && *origin)
        offset_ns = (atoll(epoch) - atoll(origin)) * 1000000000LL;
    else
        offset_ns = atoll(epoch) * 1000000000LL - now_ns(CLOCK_REALTIME);
    active = 1;
}

int clock_gettime(clockid_t id, struct timespec *ts) {
    if (!real_clock_gettime)
        real_clock_gettime = (int (*)(clockid_t, struct timespec *))dlsym(RTLD_NEXT, "clock_gettime");
    int rc = real_clock_gettime(id, ts);
    if (rc == 0 && active && (id == CLOCK_REALTIME || id == CLOCK_REALTIME_COARSE)) {
        long long v = (long long)ts->tv_sec * 1000000000LL + ts->tv_nsec + offset_ns;
        ts->tv_sec = v / 1000000000LL;
        ts->tv_nsec = v % 1000000000LL;
    }
    return rc;
}

int gettimeofday(struct timeval *tv, void *tz) {
    struct timespec ts;
    (void)tz;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct timeval *volatile out = tv;     /* glibc declares tv nonnull; callers may still pass NULL */
    if (out) {
        tv->tv_sec = ts.tv_sec;
        tv->tv_usec = ts.tv_nsec / 1000;
    }
    return 0;
}

time_t time(time_t *t) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    if (t)
        *t = ts.tv_sec;
    return ts.tv_sec;
}
