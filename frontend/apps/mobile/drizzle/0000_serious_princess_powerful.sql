CREATE TABLE `cards` (
	`id` text PRIMARY KEY NOT NULL,
	`note_id` text NOT NULL,
	`deck_id` text NOT NULL,
	`template_ordinal` integer NOT NULL,
	`state` text NOT NULL,
	`due` integer NOT NULL,
	`stability` real NOT NULL,
	`difficulty` real NOT NULL,
	`elapsed_days` real NOT NULL,
	`scheduled_days` real NOT NULL,
	`learning_steps` integer NOT NULL,
	`reps` integer NOT NULL,
	`lapses` integer NOT NULL,
	`last_reviewed_at` integer,
	`is_suspended` integer DEFAULT false NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	FOREIGN KEY (`note_id`) REFERENCES `notes`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`deck_id`) REFERENCES `decks`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `cards_due_idx` ON `cards` (`deck_id`,`is_suspended`,`due`);--> statement-breakpoint
CREATE INDEX `cards_note_id_idx` ON `cards` (`note_id`);--> statement-breakpoint
CREATE UNIQUE INDEX `cards_note_template_unq` ON `cards` (`note_id`,`template_ordinal`);--> statement-breakpoint
CREATE TABLE `decks` (
	`id` text PRIMARY KEY NOT NULL,
	`title` text NOT NULL,
	`description` text,
	`source_kind` text NOT NULL,
	`generation_job_id` text,
	`is_archived` integer DEFAULT false NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `decks_updated_at_idx` ON `decks` (`updated_at`);--> statement-breakpoint
CREATE TABLE `media` (
	`id` text PRIMARY KEY NOT NULL,
	`note_id` text NOT NULL,
	`kind` text NOT NULL,
	`local_uri` text,
	`remote_url` text,
	`alt` text,
	`width` integer,
	`height` integer,
	`license` text,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	FOREIGN KEY (`note_id`) REFERENCES `notes`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `media_note_id_idx` ON `media` (`note_id`);--> statement-breakpoint
CREATE TABLE `notes` (
	`id` text PRIMARY KEY NOT NULL,
	`deck_id` text NOT NULL,
	`note_type` text NOT NULL,
	`fields` text NOT NULL,
	`tags` text DEFAULT '[]' NOT NULL,
	`sources` text DEFAULT '[]' NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	FOREIGN KEY (`deck_id`) REFERENCES `decks`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `notes_deck_id_idx` ON `notes` (`deck_id`);--> statement-breakpoint
CREATE TABLE `reviews` (
	`id` text PRIMARY KEY NOT NULL,
	`card_id` text NOT NULL,
	`rating` integer NOT NULL,
	`state` text NOT NULL,
	`due` integer NOT NULL,
	`stability` real NOT NULL,
	`difficulty` real NOT NULL,
	`elapsed_days` real NOT NULL,
	`last_elapsed_days` real NOT NULL,
	`scheduled_days` real NOT NULL,
	`reviewed_at` integer NOT NULL,
	`duration_ms` integer NOT NULL,
	FOREIGN KEY (`card_id`) REFERENCES `cards`(`id`) ON UPDATE no action ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX `reviews_card_id_idx` ON `reviews` (`card_id`);--> statement-breakpoint
CREATE INDEX `reviews_reviewed_at_idx` ON `reviews` (`reviewed_at`);