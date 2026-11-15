import { UserRole } from '@prisma/client';
export declare const PUBLIC_ROUTE = "publicRoute";
export declare const ROLES = "roles";
export declare const Public: () => import("@nestjs/common").CustomDecorator<string>;
export declare const Roles: (...roles: UserRole[]) => import("@nestjs/common").CustomDecorator<string>;
export declare const CurrentUser: (...dataOrPipes: unknown[]) => ParameterDecorator;
