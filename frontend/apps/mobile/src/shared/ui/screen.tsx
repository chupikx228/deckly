import type { ReactNode } from 'react';
import { ScrollView, View } from 'react-native';
import { SafeAreaView, type Edge } from 'react-native-safe-area-context';

export interface ScreenProps {
  children: ReactNode;
  header?: ReactNode | undefined;
  footer?: ReactNode | undefined;
  scrollable?: boolean;
  edges?: readonly Edge[];
  className?: string | undefined;
}

const DEFAULT_EDGES: readonly Edge[] = ['top'];

export const Screen = ({
  children,
  header,
  footer,
  scrollable = false,
  edges = DEFAULT_EDGES,
  className,
}: ScreenProps) => {
  const content = scrollable ? (
    <ScrollView
      className="flex-1"
      contentContainerClassName="px-gutter pb-section pt-1 gap-4"
      keyboardShouldPersistTaps="handled"
      showsVerticalScrollIndicator={false}
    >
      {children}
    </ScrollView>
  ) : (
    <View className={['flex-1 px-gutter', className ?? ''].join(' ')}>{children}</View>
  );

  return (
    <SafeAreaView className="flex-1 bg-background" edges={edges}>
      {header === undefined ? null : <View className="px-gutter pt-2">{header}</View>}

      {content}

      {footer === undefined ? null : <View className="px-gutter pb-2 pt-3">{footer}</View>}
    </SafeAreaView>
  );
};
