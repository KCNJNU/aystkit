import { ThrottlerGuard } from '@nestjs/throttler';
export declare class LoginRateLimitGuard extends ThrottlerGuard {
    protected getTracker(request: Record<string, unknown>): Promise<string>;
}
