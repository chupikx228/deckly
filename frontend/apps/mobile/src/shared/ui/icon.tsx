import { Ionicons } from '@expo/vector-icons';
import { cssInterop } from 'nativewind';

export const Icon = cssInterop(Ionicons, {
  className: { target: false, nativeStyleToProp: { color: true } },
});
