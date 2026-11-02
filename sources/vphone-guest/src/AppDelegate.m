#import "AppDelegate.h"
#import "ViewController.h"
#import <unistd.h>

@implementation AppDelegate

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    NSArray *searchPaths = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES);
    NSString *docsDirectory = [searchPaths firstObject];
    NSString *logPath = [docsDirectory stringByAppendingPathComponent:@"hello.log"];

    NSDateFormatter *dateFormatter = [[[NSDateFormatter alloc] init] autorelease];
    [dateFormatter setDateFormat:@"yyyy-MM-dd'T'HH:mm:ssZZZZZ"];
    NSString *isoTimestamp = [dateFormatter stringFromDate:[NSDate date]];

    pid_t currentPid = getpid();
    NSString *mainBundlePath = [[NSBundle mainBundle] bundlePath];

    NSString *traceLine = [NSString stringWithFormat:@"[%@] pid=%d bundle=%@\n",
                           isoTimestamp, (int)currentPid, mainBundlePath];

    NSLog(@"[vphone-hello] %@", traceLine);

    NSFileManager *fileManager = [NSFileManager defaultManager];
    if (![fileManager fileExistsAtPath:logPath]) {
        [traceLine writeToFile:logPath atomically:YES encoding:NSUTF8StringEncoding error:NULL];
    } else {
        NSFileHandle *fileHandle = [NSFileHandle fileHandleForWritingAtPath:logPath];
        if (fileHandle) {
            [fileHandle seekToEndOfFile];
            NSData *lineData = [traceLine dataUsingEncoding:NSUTF8StringEncoding];
            if (lineData) {
                [fileHandle writeData:lineData];
            }
            [fileHandle closeFile];
        }
    }

    self.window = [[[UIWindow alloc] initWithFrame:[[UIScreen mainScreen] bounds]] autorelease];
    self.window.backgroundColor = [UIColor blackColor];

    ViewController *rootVC = [[[ViewController alloc] init] autorelease];
    self.window.rootViewController = rootVC;
    [self.window makeKeyAndVisible];

    return YES;
}

- (void)dealloc {
    [_window release];
    [super dealloc];
}

@end
