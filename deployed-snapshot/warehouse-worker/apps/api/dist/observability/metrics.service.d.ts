import { Counter, Gauge, Histogram, Registry } from 'prom-client';
import { PrismaService } from '../prisma/prisma.service';
export declare class MetricsService {
    private readonly prisma;
    readonly registry: Registry<"text/plain; version=0.0.4; charset=utf-8">;
    readonly httpRequests: Counter<"method" | "route" | "status_code">;
    readonly httpDuration: Histogram<"method" | "route" | "status_code">;
    readonly jobBacklog: Gauge<"status">;
    readonly refreshSuccessful: Gauge<string>;
    constructor(prisma: PrismaService);
    observeHttp(method: string, route: string, statusCode: number, seconds: number): void;
    render(): Promise<string>;
    private refreshPlatformGauges;
}
