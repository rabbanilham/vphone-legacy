/*
 * vphoned_legacy.m — Guest agent for vphone-legacy (iOS 7 - 9).
 *
 * Runs inside the emulated legacy iOS guest as a LaunchDaemon.
 * Communicates with the host over TCP port 1337 using length-prefixed JSON.
 */

#import <Foundation/Foundation.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/utsname.h>

#define VPHONE_LEGACY_PORT 1337
#define PROTOCOL_VERSION 1

static BOOL read_fully(int fd, void *buf, size_t count) {
    size_t total = 0;
    while (total < count) {
        ssize_t n = read(fd, (char *)buf + total, count - total);
        if (n <= 0) return NO;
        total += n;
    }
    return YES;
}

static BOOL write_fully(int fd, const void *buf, size_t count) {
    size_t total = 0;
    while (total < count) {
        ssize_t n = write(fd, (const char *)buf + total, count - total);
        if (n <= 0) return NO;
        total += n;
    }
    return YES;
}

static NSDictionary *read_json_message(int fd) {
    uint32_t len_be = 0;
    if (!read_fully(fd, &len_be, sizeof(len_be))) return nil;
    uint32_t len = ntohl(len_be);
    if (len == 0 || len > 10 * 1024 * 1024) return nil;

    NSMutableData *data = [NSMutableData dataWithLength:len];
    if (!read_fully(fd, [data mutableBytes], len)) return nil;

    NSError *err = nil;
    return [NSJSONSerialization JSONObjectWithData:data options:0 error:&err];
}

static BOOL write_json_message(int fd, NSDictionary *dict) {
    NSError *err = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:dict options:0 error:&err];
    if (!data) return NO;

    uint32_t len_be = htonl((uint32_t)[data length]);
    if (!write_fully(fd, &len_be, sizeof(len_be))) return NO;
    return write_fully(fd, [data bytes], [data length]);
}

static NSDictionary *handle_command(NSDictionary *req) {
    NSString *cmd = req[@"cmd"] ?: @"";
    NSMutableDictionary *res = [NSMutableDictionary dictionary];
    res[@"protocol"] = @(PROTOCOL_VERSION);
    res[@"id"] = req[@"id"] ?: @0;

    if ([cmd isEqualToString:@"ping"]) {
        res[@"status"] = @"pong";
        res[@"agent"] = @"vphoned-legacy";
        res[@"version"] = @"1.0";
        return res;
    }

    if ([cmd isEqualToString:@"info"]) {
        struct utsname u;
        uname(&u);
        res[@"status"] = @"ok";
        res[@"sysname"] = [NSString stringWithUTF8String:u.sysname];
        res[@"nodename"] = [NSString stringWithUTF8String:u.nodename];
        res[@"release"] = [NSString stringWithUTF8String:u.release];
        res[@"version"] = [NSString stringWithUTF8String:u.version];
        res[@"machine"] = [NSString stringWithUTF8String:u.machine];
        res[@"hostname"] = [[NSProcessInfo processInfo] hostName];
        return res;
    }

    if ([cmd isEqualToString:@"exec"]) {
        NSString *sh = req[@"command"] ?: @"";
        FILE *fp = popen([sh UTF8String], "r");
        if (!fp) {
            res[@"status"] = @"error";
            res[@"error"] = @"Failed to popen";
            return res;
        }
        char buf[1024];
        NSMutableString *outStr = [NSMutableString string];
        while (fgets(buf, sizeof(buf), fp) != NULL) {
            [outStr appendString:[NSString stringWithUTF8String:buf]];
        }
        int code = pclose(fp);
        res[@"status"] = @"ok";
        res[@"output"] = outStr;
        res[@"exit_code"] = @(code);
        return res;
    }

    if ([cmd isEqualToString:@"file_read"]) {
        NSString *path = req[@"path"] ?: @"";
        NSData *content = [NSData dataWithContentsOfFile:path];
        if (content) {
            res[@"status"] = @"ok";
            res[@"data"] = [content base64EncodedStringWithOptions:0];
        } else {
            res[@"status"] = @"error";
            res[@"error"] = @"File not found or unreadable";
        }
        return res;
    }

    if ([cmd isEqualToString:@"file_write"]) {
        NSString *path = req[@"path"] ?: @"";
        NSString *b64 = req[@"data"] ?: @"";
        NSData *content = [[NSData alloc] initWithBase64EncodedString:b64 options:0];
        if (content && [content writeToFile:path atomically:YES]) {
            res[@"status"] = @"ok";
        } else {
            res[@"status"] = @"error";
            res[@"error"] = @"Failed to write file";
        }
        return res;
    }

    res[@"status"] = @"error";
    res[@"error"] = [NSString stringWithFormat:@"Unknown command: %@", cmd];
    return res;
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSLog(@"[vphoned-legacy] Starting daemon on TCP port %d...", VPHONE_LEGACY_PORT);
        int server_fd = socket(AF_INET, SOCK_STREAM, 0);
        if (server_fd < 0) {
            perror("socket failed");
            return 1;
        }

        int opt = 1;
        setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

        struct sockaddr_in address;
        memset(&address, 0, sizeof(address));
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = INADDR_ANY;
        address.sin_port = htons(VPHONE_LEGACY_PORT);

        if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
            perror("bind failed");
            return 1;
        }

        if (listen(server_fd, 5) < 0) {
            perror("listen failed");
            return 1;
        }

        NSLog(@"[vphoned-legacy] Listening for host connections...");

        while (YES) {
            struct sockaddr_in client_addr;
            socklen_t addr_len = sizeof(client_addr);
            int client_fd = accept(server_fd, (struct sockaddr *)&client_addr, &addr_len);
            if (client_fd < 0) continue;

            NSLog(@"[vphoned-legacy] Client connected: %s", inet_ntoa(client_addr.sin_addr));

            while (YES) {
                NSDictionary *req = read_json_message(client_fd);
                if (!req) break;

                NSDictionary *resp = handle_command(req);
                if (!write_json_message(client_fd, resp)) break;
            }

            close(client_fd);
            NSLog(@"[vphoned-legacy] Client disconnected.");
        }
    }
    return 0;
}
