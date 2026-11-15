"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.isSuperAdmin = exports.isGlobalAdmin = exports.DataReader = exports.canReadData = exports.DATA_READ_ROLES = void 0;
exports.assignableRoles = assignableRoles;
const client_1 = require("@prisma/client");
const auth_decorators_1 = require("./auth.decorators");
exports.DATA_READ_ROLES = [client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR, client_1.UserRole.VIEWER, client_1.UserRole.MAINTENANCE_ENGINEER];
const canReadData = (role) => exports.DATA_READ_ROLES.includes(role);
exports.canReadData = canReadData;
const DataReader = () => (0, auth_decorators_1.Roles)(...exports.DATA_READ_ROLES);
exports.DataReader = DataReader;
const isGlobalAdmin = (role) => [client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN].includes(role);
exports.isGlobalAdmin = isGlobalAdmin;
const isSuperAdmin = (role) => role === client_1.UserRole.ADMIN || role === client_1.UserRole.SUPER_ADMIN;
exports.isSuperAdmin = isSuperAdmin;
function assignableRoles(role) {
    const scoped = [client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR, client_1.UserRole.VIEWER, client_1.UserRole.BASIC];
    if ((0, exports.isSuperAdmin)(role))
        return [client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, ...scoped];
    if (role === client_1.UserRole.SYSTEM_ADMIN)
        return [client_1.UserRole.TENANT_ADMIN, ...scoped];
    return role === client_1.UserRole.TENANT_ADMIN ? scoped : [];
}
//# sourceMappingURL=permissions.js.map