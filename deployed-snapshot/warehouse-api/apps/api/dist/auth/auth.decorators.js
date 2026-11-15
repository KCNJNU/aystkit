"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.CurrentUser = exports.Roles = exports.Public = exports.ROLES = exports.PUBLIC_ROUTE = void 0;
const common_1 = require("@nestjs/common");
exports.PUBLIC_ROUTE = 'publicRoute';
exports.ROLES = 'roles';
const Public = () => (0, common_1.SetMetadata)(exports.PUBLIC_ROUTE, true);
exports.Public = Public;
const Roles = (...roles) => (0, common_1.SetMetadata)(exports.ROLES, roles);
exports.Roles = Roles;
exports.CurrentUser = (0, common_1.createParamDecorator)((_data, context) => context.switchToHttp().getRequest().user);
//# sourceMappingURL=auth.decorators.js.map