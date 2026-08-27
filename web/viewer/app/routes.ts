import { type RouteConfig, index, route } from "@react-router/dev/routes";

export default [
  index("routes/home.tsx"),
  route("talk", "routes/talk.tsx"),
  route("talk/:identityId", "routes/talk-chat.tsx"),
  route("i/:identityId", "routes/timeline.tsx"),
  route("i/:identityId/recap", "routes/recap.tsx"),
  route("i/:identityId/mindlog", "routes/identity.tsx"),
  route("i/:identityId/mindlog2", "routes/mindlog2.tsx"),
  route("i/:identityId/t/:trajId", "routes/sub-traj.tsx"),
  route("i/:identityId/thinkers", "routes/thinkers.tsx"),
  route("i/:identityId/health", "routes/health.tsx"),
  route("i/:identityId/usage", "routes/usage.tsx"),
  route("i/:identityId/chat", "routes/chat.tsx"),
  route("i/:identityId/memories", "routes/memories.tsx"),
  route("i/:identityId/proposals", "routes/proposals.tsx"),
  route("i/:identityId/shadow", "routes/shadow.tsx"),
  route("i/:identityId/config", "routes/config.tsx"),
] satisfies RouteConfig;
