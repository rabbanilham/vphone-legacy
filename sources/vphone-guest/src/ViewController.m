#import "ViewController.h"
#import <QuartzCore/QuartzCore.h>

@implementation ViewController

- (void)viewDidLoad {
    [super viewDidLoad];

    self.view.backgroundColor = [UIColor colorWithWhite:0.08 alpha:1.0];

    CGRect screenBounds = self.view.bounds;
    CGFloat labelWidth = screenBounds.size.width - 40.0;
    CGFloat labelHeight = 70.0;
    CGFloat labelX = 20.0;
    CGFloat labelY = (screenBounds.size.height - labelHeight) / 2.0;

    UILabel *greetingLabel = [[[UILabel alloc] initWithFrame:CGRectMake(labelX, labelY, labelWidth, labelHeight)] autorelease];
    greetingLabel.text = @"Hello from vphone";
    greetingLabel.textColor = [UIColor whiteColor];
    greetingLabel.backgroundColor = [UIColor colorWithRed:0.0 green:0.478 blue:1.0 alpha:1.0];
    greetingLabel.textAlignment = NSTextAlignmentCenter;
    greetingLabel.font = [UIFont boldSystemFontOfSize:22.0];
    greetingLabel.layer.cornerRadius = 12.0;
    greetingLabel.layer.masksToBounds = YES;
    greetingLabel.autoresizingMask = UIViewAutoresizingFlexibleLeftMargin |
                                    UIViewAutoresizingFlexibleRightMargin |
                                    UIViewAutoresizingFlexibleTopMargin |
                                    UIViewAutoresizingFlexibleBottomMargin |
                                    UIViewAutoresizingFlexibleWidth;

    [self.view addSubview:greetingLabel];
}

- (UIStatusBarStyle)preferredStatusBarStyle {
    return UIStatusBarStyleLightContent;
}

@end
