/* Diagnostic guest client. Uses the same liblockdown API as SpringBoard.
 * Minimal ABI declarations permit building against exported-symbol stubs without
 * an iOS SDK; no macOS headers or implementations are linked into the guest.
 */
typedef const void *CFTypeRef;
typedef const void *CFStringRef;
typedef unsigned long CFTypeID;
typedef long CFIndex;
extern CFStringRef CFStringCreateWithCString(void *, const char *, unsigned int);
extern int CFStringGetCString(CFStringRef, char *, CFIndex, unsigned int);
extern CFTypeID CFGetTypeID(CFTypeRef);
extern CFTypeID CFStringGetTypeID(void);
extern CFTypeID CFBooleanGetTypeID(void);
extern unsigned char CFBooleanGetValue(CFTypeRef);
extern void CFRelease(CFTypeRef);
extern void *lockdown_connect(void);
extern CFTypeRef lockdown_copy_value(void *, CFStringRef, CFStringRef);
extern void lockdown_disconnect(void *);
extern unsigned int sleep(unsigned int);
extern int open(const char *, int, ...);
extern long write(int, const void *, unsigned long);
extern int close(int);
extern int snprintf(char *, unsigned long, const char *, ...);
extern int fork(void);
extern int execv(const char *, char *const *);
extern int waitpid(int, int *, int);
extern int dup2(int, int);
extern void _exit(int);
extern int getpid(void);
extern int *__error(void);
extern void SBSUndimScreen(void);

#define UTF8 0x08000100U
#define O_WRONLY 1
#define O_CREAT 0x200
#define O_TRUNC 0x400
#define O_APPEND 8
#define REPORT "/private/var/root/virtual-activation-probe.jsonl"

static int launchctl(const char *verb, const char *output) {
    int pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        int fd = open(output, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd < 0) _exit(125);
        dup2(fd, 1); dup2(fd, 2); close(fd);
        char *args[] = {"/bin/launchctl", (char *)verb,
                        "com.apple.mobile.lockdown", (void *)0};
        execv(args[0], args);
        _exit(126);
    }
    int status = -1;
    return waitpid(pid, &status, 0) == pid ? status : -1;
}

int main(void) {
    CFStringRef ak = CFStringCreateWithCString(0, "ActivationState", UTF8);
    CFStringRef bk = CFStringCreateWithCString(0, "BrickState", UTF8);
    if (!ak || !bk) return 2;
    sleep(20);
    int good = 0, restarted = 0;
    for (int sample = 0; sample < 45; ++sample) {
        void *conn = lockdown_connect();
        CFTypeRef a = conn ? lockdown_copy_value(conn, 0, ak) : 0;
        CFTypeRef b = conn ? lockdown_copy_value(conn, 0, bk) : 0;
        char state[64] = "unavailable", line[640];
        int valid_a = a && CFGetTypeID(a) == CFStringGetTypeID();
        int valid_b = b && CFGetTypeID(b) == CFBooleanGetTypeID();
        if (valid_a && !CFStringGetCString(a, state, sizeof(state), UTF8)) valid_a = 0;
        /* Activation-state strings in this build are simple ASCII identifiers. */
        if (valid_a) {
            for (unsigned int j = 0; state[j]; ++j)
                if (!((state[j] >= 'A' && state[j] <= 'Z') ||
                      (state[j] >= 'a' && state[j] <= 'z'))) valid_a = 0;
        }
        if (!valid_a) { state[0] = '?'; state[1] = 0; }
        int n = snprintf(line, sizeof(line),
            "{\"probe_pid\":%d,\"sample\":%d,\"after_restart\":%s,"
            "\"api\":\"liblockdown\",\"ActivationState\":\"%s\","
            "\"BrickState\":%s,\"valid\":%s,\"connected\":%s,"
            "\"a_present\":%s,\"b_present\":%s,\"errno\":%d,"
            "\"a_type\":%lu,\"b_type\":%lu,\"string_type\":%lu,\"boolean_type\":%lu}\n",
            getpid(), sample, restarted ? "true" : "false", state,
            valid_b ? (CFBooleanGetValue(b) ? "true" : "false") : "null",
            valid_a && valid_b ? "true" : "false", conn ? "true" : "false",
            a ? "true" : "false", b ? "true" : "false", *__error(),
            a ? CFGetTypeID(a) : 0, b ? CFGetTypeID(b) : 0,
            CFStringGetTypeID(), CFBooleanGetTypeID());
        int fd = open(REPORT, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd >= 0) { if (n > 0 && n < (int)sizeof(line)) write(fd, line, n); close(fd); }
        if (n > 0 && n < (int)sizeof(line)) write(1, line, n);
        if (valid_a && valid_b) ++good;
        if (a) CFRelease(a);
        if (b) CFRelease(b);
        if (conn) lockdown_disconnect(conn);
        /* Native display wake only: unlocking remains normal touch input. */
        SBSUndimScreen();
        if (!restarted && good >= 4) {
            launchctl("list", "/private/var/root/virtual-activation-before-restart.txt");
            int status = launchctl("stop", "/private/var/root/virtual-activation-stop.txt");
            if (status == 0) {
                restarted = 1;
                sleep(3);
                /* The next native connection triggers socket activation. */
            }
        }
        if (restarted && good >= 6)
            launchctl("list", "/private/var/root/virtual-activation-after-restart.txt");
        sleep(3);
    }
    CFRelease(ak); CFRelease(bk);
    return 0;
}
