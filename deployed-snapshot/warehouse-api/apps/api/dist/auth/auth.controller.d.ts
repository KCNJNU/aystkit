import { Response } from 'express';
import { AuthService } from './auth.service';
import { AuthenticatedRequest, AuthUser } from './auth.types';
export declare class LoginDto {
    email: string;
    password: string;
}
declare class ChangePasswordDto {
    currentPassword: string;
    newPassword: string;
}
export declare class AuthController {
    private readonly auth;
    constructor(auth: AuthService);
    login(body: LoginDto, response: Response): Promise<{
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
        csrfToken: string;
        expiresAt: Date;
    }>;
    session(user: AuthUser, request: AuthenticatedRequest): Promise<{
        user: AuthUser;
        csrfToken: string;
    }>;
    logout(request: AuthenticatedRequest, response: Response): Promise<{
        success: boolean;
    }>;
    changePassword(user: AuthUser, body: ChangePasswordDto, response: Response): Promise<{
        success: boolean;
        reauthenticate: boolean;
    }>;
}
export {};
