import { ConfigService } from '@nestjs/config';
import { PrismaService } from '../prisma/prisma.service';
export declare class AuthService {
    private readonly prisma;
    private readonly config;
    constructor(prisma: PrismaService, config: ConfigService);
    login(emailInput: string, password: string): Promise<{
        user: {
            id: string;
            email: string;
            displayName: string;
            role: import(".prisma/client").$Enums.UserRole;
            tenantId: string | null;
            projectIds: string[];
            mustChangePassword: boolean;
            isActive: boolean;
        };
        sessionToken: string;
        csrfToken: string;
        sessionId: string;
        expiresAt: Date;
    }>;
    resolveSession(token: string): Promise<{
        sessionId: string;
        csrfTokenHash: string;
        user: {
            id: string;
            email: string;
            displayName: string;
            role: import(".prisma/client").$Enums.UserRole;
            tenantId: string | null;
            projectIds: string[];
            mustChangePassword: boolean;
            isActive: boolean;
        };
    } | null>;
    rotateCsrf(sessionId: string): Promise<string>;
    verifyCsrf(value: string | undefined, expectedHash: string | undefined): boolean;
    logout(sessionId: string): Promise<void>;
    changePassword(userId: string, currentPassword: string, newPassword: string): Promise<void>;
    cookieOptions(): {
        httpOnly: boolean;
        sameSite: "lax";
        secure: boolean;
        maxAge: number;
        path: string;
    };
    private sessionHours;
}
